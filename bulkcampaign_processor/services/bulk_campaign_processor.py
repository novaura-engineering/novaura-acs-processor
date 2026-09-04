import logging
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from twilio.rest import Client
import pytz
from enum import Enum
from datetime import timedelta

from external_models.models.nurturing_campaigns import (
    LeadNurturingParticipant,
    BulkCampaignMessage,
)

from external_models.models.drip_campaigns import (
    DripCampaignProgress
)

from external_models.models.reminder_campaigns import (
    ReminderCampaignProgress,
)

from external_models.models.blast_campaigns import (
    BlastCampaignProgress,
)

from external_models.models.communications import (
    Participant
)

from django.conf import settings
import time

from shared_services.message_delivery import MessageDeliveryService
from shared_services.nurturing_attribution import resolve_media_campaign_for_participant
from shared_services.template_variable_render import build_nested_template_context
from shared_services.message_validation_service import MessageValidationService
from shared_services.time_calculation_service import TimeCalculationService
from shared_services.message_group_service import MessageGroupService

from link_tracking.services.runtime_publisher import ensure_link_published
from bulkcampaign_processor.utils.timezone_utils import convert_from_utc
from bulkcampaign_processor.utils.short_link import build_bulk_short_url
from bulkcampaign_processor.services.send_cap_service import (
    clear_send_cap_claim_metadata,
    reconcile_stale_send_cap_claims,
    refund_send_slot,
    should_refund_after_send_failure,
    try_claim_send_slot,
)
from sms_marketing.models import SmsSubscriberCampaignSubscription

logger = logging.getLogger(__name__)

class SendOutcome(Enum):
    """Why a send attempt ended, so callers can tell "not tried" from "tried and failed".

    _send_message used to return a bare bool, and returned False both for a message that
    was never attempted (rate limited, outside business hours, campaign paused) and for
    one that was attempted and failed. process_retry_messages treated every False as a
    delivery failure and spent one of the message's three retries on it, so three send-cap
    deferrals were enough to mark a message failed_final -- overwriting the `scheduled`
    status the deferral had just set, and dropping a message that was only ever waiting
    for capacity.

    DEFERRED means nothing was sent and nothing failed: leave the message alone and let
    the normal due/retry sweep pick it up when it is eligible again. Only FAILED should
    consume a retry.
    """

    SENT = 'sent'
    DEFERRED = 'deferred'
    FAILED = 'failed'


class BulkCampaignProcessor:
    """
    Service class for processing bulk nurturing campaigns
    Handles drip campaigns, reminder campaigns, and blast campaigns
    """

    def __init__(self):
        self.campaign_processors = {
            'drip': self._process_drip_campaign,
            'reminder': self._process_reminder_campaign,
            'blast': self._process_blast_campaign
        }
        self.message_delivery = MessageDeliveryService()
        self.validator = MessageValidationService(self.message_delivery)
        self.time_calculator = TimeCalculationService()
        self.message_group = MessageGroupService()

    def _keyword_from_rule(self, rule):
        """Get keyword text from SmsKeywordRule (rule.keyword.keyword)."""
        if not rule:
            return ''
        kw = getattr(rule, 'keyword', None)
        return getattr(kw, 'keyword', '') if kw else ''

    def _resolve_link_for_bulk_message(self, message, campaign):
        """
        Resolve the Link and opt-in keyword for a drip, reminder, or blast message.
        Drip/Reminder/Blast: When use_opt_in_rule_link is True, prefer opt-in rule link from
        participant.originating_subscription.opt_in_rule.short_link (and keyword from that rule),
        then fallback to the step's/reminder's/schedule's fixed short_link. When False, use the
        fixed short_link only. Keyword is resolved from the rule used for the link when available,
        or from originating_subscription.opt_in_rule when participant exists (for {{keyword.keyword}} and UTM).
        Returns (link, drip_step_id, reminder_message_id, blast_schedule_id, keyword_str).
        """
        drip_step_id = None
        reminder_message_id = None
        blast_schedule_id = None
        fixed_link = None
        use_opt_in = False

        if campaign.campaign_type == 'drip' and message.drip_message_step:
            step = message.drip_message_step
            drip_step_id = step.id
            fixed_link = getattr(step, 'short_link', None) if getattr(step, 'short_link_id', None) else None
            use_opt_in = getattr(step, 'use_opt_in_rule_link', False)
        elif campaign.campaign_type == 'reminder' and message.reminder_message:
            rem = message.reminder_message
            reminder_message_id = rem.id
            fixed_link = getattr(rem, 'short_link', None) if getattr(rem, 'short_link_id', None) else None
            use_opt_in = getattr(rem, 'use_opt_in_rule_link', False)
        elif campaign.campaign_type == 'blast':
            schedule = getattr(campaign, 'blast_schedule', None)
            if schedule:
                blast_schedule_id = schedule.id
                fixed_link = getattr(schedule, 'short_link', None) if getattr(schedule, 'short_link_id', None) else None
                # Same as drip/reminder: explicit flag to use participant's opt-in rule link and keyword
                use_opt_in = getattr(schedule, 'use_opt_in_rule_link', False)
            else:
                use_opt_in = False
        else:
            return (None, None, None, None, '')

        participant = getattr(message, 'participant', None)
        link = fixed_link if not use_opt_in else None
        rule_for_keyword = None

        if use_opt_in and participant:
            # Prefer originating_subscription (campaign-scoped opt-in): subscription.opt_in_rule -> short_link
            subscription = getattr(participant, 'originating_subscription', None)
            if subscription and getattr(subscription, 'opt_in_rule_id', None):
                rule = getattr(subscription, 'opt_in_rule', None)
                if rule and getattr(rule, 'short_link_id', None):
                    link = getattr(rule, 'short_link', None)
                    rule_for_keyword = rule

            if link is None:
                link = fixed_link
                # Log when use_opt_in_rule_link was set but we couldn't resolve a per-participant link
                if campaign.campaign_type in ('drip', 'reminder', 'blast'):
                    orig_sub_id = getattr(participant, 'originating_subscription_id', None)
                    if orig_sub_id is not None:
                        reason = []
                        if subscription is None:
                            reason.append('originating_subscription not found')
                        elif getattr(subscription, 'opt_in_rule_id', None) is None:
                            reason.append('subscription.opt_in_rule_id is null')
                        else:
                            rule = getattr(subscription, 'opt_in_rule', None)
                            if rule is None:
                                reason.append('opt_in_rule not found')
                            elif getattr(rule, 'short_link_id', None) is None:
                                reason.append('opt_in_rule.short_link_id is null')
                            else:
                                reason.append('opt_in_rule.short_link not found')
                        if fixed_link is None:
                            reason.append('no schedule/step fixed short_link fallback')
                        logger.warning(
                            "use_opt_in_rule_link=True but link not resolved for %s participant_id=%s "
                            "originating_subscription_id=%s: %s",
                            campaign.campaign_type,
                            participant.id,
                            orig_sub_id,
                            "; ".join(reason),
                        )

        # Resolve keyword for UTM and body: from rule when we have it, else from originating_subscription
        if rule_for_keyword:
            keyword_str = self._keyword_from_rule(rule_for_keyword)
        elif participant:
            subscription = getattr(participant, 'originating_subscription', None)
            if subscription and getattr(subscription, 'opt_in_rule_id', None):
                rule_for_keyword = getattr(subscription, 'opt_in_rule', None)
            keyword_str = self._keyword_from_rule(rule_for_keyword) if rule_for_keyword else ''
        else:
            keyword_str = ''

        return (link, drip_step_id, reminder_message_id, blast_schedule_id, keyword_str)

    def process_campaign(self, campaign):
        """
        Process a bulk nurturing campaign based on its type

        Args:
            campaign: LeadNurturingCampaign instance

        Returns:
            int: Number of messages scheduled/sent
        """
        if not campaign.is_active_or_scheduled():
            logger.warning(f"Skipping inactive campaign {campaign.id} - Status: {campaign.status}")
            return 0

        processor = self.campaign_processors.get(campaign.campaign_type)
        if not processor:
            logger.error(f"No processor found for campaign type: {campaign.campaign_type}")
            return 0

        try:
            result = processor(campaign)
            return result
        except Exception as e:
            logger.exception(f"Error processing campaign {campaign.id}: {str(e)}")
            return 0

    def process_due_messages(self):
        """
        Process all messages that are due to be sent
        This should be run periodically by a scheduled task

        Returns:
            int: Number of messages processed
        """
        try:
            reconciled = reconcile_stale_send_cap_claims()
            if reconciled:
                logger.info('send_cap_stale_reconciled_batch count=%s', reconciled)
        except Exception:
            logger.exception('reconcile_stale_send_cap_claims failed')

        # Find all pending messages that are due from active campaigns only
        due_messages = BulkCampaignMessage.objects.filter(
            status__in=['pending', 'scheduled', 'failed', 'retry'],  # Include retry status for retry functionality
            scheduled_for__lte=timezone.now(),
            campaign__active=True,  # Only include messages from active campaigns
            campaign__status__in=['active', 'scheduled']  # Only include messages from active or scheduled campaigns
        ).select_related(
            'campaign',
            'campaign__blast_schedule',
            'campaign__blast_schedule__short_link',
            'participant',
            'participant__lead',
            'participant__nurturing_campaign',
            'participant__nurturing_campaign__media_campaign',
            'participant__originating_subscription',
            'participant__originating_subscription__media_campaign',
            'participant__media_campaign',
            'campaign__media_campaign',
            'participant__originating_subscription__opt_in_rule',
            'participant__originating_subscription__opt_in_rule__short_link',
            'participant__originating_subscription__opt_in_rule__keyword',
            'message_group',
            'drip_message_step',
            'drip_message_step__short_link',
            'reminder_message',
            'reminder_message__short_link',
        ).order_by('scheduled_for')  # Process messages in order of scheduled time

        processed_count = 0
        processed_groups = set()  # Track which message groups we've processed

        for message in due_messages:
            try:
                # Skip if we've already processed this message group
                if message.message_group_id in processed_groups:
                    continue

                # Check if the campaign is still active before processing
                if not message.campaign.is_active_or_scheduled():
                    logger.warning(f"Skipping message {message.id} from inactive campaign {message.campaign.id} - Status: {message.campaign.status}")
                    continue

                # Handle retry logic for failed messages
                if message.status == 'failed':
                    if self._handle_failed_message_retry(message):
                        # Message was marked for retry, skip processing for now
                        continue
                    else:
                        # Message cannot be retried; mark as final failure so it is excluded from future runs
                        logger.info(f"Message {message.id} max retries exceeded, marking as failed_final")
                        message.update_status('failed_final', {'error': 'Max retries exceeded'})
                        continue

                # Get all messages in the group (prefetch link-resolution chain so
                # participant.originating_subscription → opt_in_rule → short_link is available when sending)
                related_messages = BulkCampaignMessage.objects.filter(
                    message_group=message.message_group
                ).select_related(
                    'campaign',
                    'campaign__blast_schedule',
                    'campaign__blast_schedule__short_link',
                    'participant',
                    'participant__lead',
                    'participant__nurturing_campaign',
                    'participant__nurturing_campaign__media_campaign',
                    'participant__originating_subscription',
                    'participant__originating_subscription__opt_in_rule',
                    'participant__originating_subscription__opt_in_rule__short_link',
                    'participant__originating_subscription__opt_in_rule__keyword',
                    'participant__originating_subscription__media_campaign',
                    'participant__media_campaign',
                    'campaign__media_campaign',
                    'drip_message_step',
                    'drip_message_step__short_link',
                    'reminder_message',
                    'reminder_message__short_link',
                ).order_by(
                    '-message_type',  # Descending order puts 'regular' before 'opt_out_notice'
                    'scheduled_for'  # Then by scheduled time
                )

                # Get regular and opt-out messages
                regular_message = related_messages.filter(message_type='regular').first()
                opt_out_message = related_messages.filter(message_type='opt_out_notice').first()

                # Validate messages before sending
                if not self.validator.validate_message_pair(regular_message, opt_out_message):
                    # Update message group status
                    self.message_group.update_group_status(
                        message.message_group,
                        'failed',
                        'Message validation failed before sending'
                    )

                    # Update individual message statuses
                    related_messages.update(
                        status='failed',
                        error_message='Message validation failed before sending',
                        updated_at=timezone.now()
                    )
                    logger.warning(f"Messages in group {message.message_group_id} failed validation")
                    continue

                # If messages were previously in failed state, update their status
                if message.message_group.status == 'failed':
                    self.message_group.update_group_status(
                        message.message_group,
                        'pending',
                        None  # Clear error message
                    )
                    related_messages.update(
                        status='scheduled',
                        error_message=None,
                        updated_at=timezone.now()
                    )
                    logger.info(f"Retrying messages in group {message.message_group_id} that were previously failed")

                # Process all messages in the group atomically
                with transaction.atomic():
                    all_success = True
                    any_failed = False
                    for related_message in related_messages:
                        outcome = self._send_message(related_message)
                        if outcome is not SendOutcome.SENT:
                            all_success = False
                            # A deferred message has not failed -- it is waiting for
                            # capacity or for its window. Stop working the group, but do
                            # not brand it failed, or the next sweep sees a failure that
                            # never happened.
                            any_failed = outcome is SendOutcome.FAILED
                            break

                    if any_failed:
                        # If any message failed, mark the group as failed instead of cancelled
                        self.message_group.update_group_status(
                            message.message_group,
                            'failed',
                            f'Message {related_message.id} failed to send'
                        )
                        related_messages.update(
                            status='failed',
                            error_message='Message failed to send',
                            updated_at=timezone.now()
                        )
                        logger.error(f"Failed to send messages in group {message.message_group_id}")
                    else:
                        processed_count += related_messages.count()

                # Mark this message group as processed
                processed_groups.add(message.message_group_id)

            except Exception as e:
                logger.exception(f"Error processing messages in group {message.message_group_id}: {e}")
                # Mark the group as failed instead of cancelled
                self.message_group.update_group_status(
                    message.message_group,
                    'failed',
                    f'Error processing messages: {str(e)}'
                )
                BulkCampaignMessage.objects.filter(
                    message_group=message.message_group
                ).update(
                    status='failed',
                    error_message=f'Error processing messages: {str(e)}',
                    updated_at=timezone.now()
                )

        return processed_count

    def process_retry_messages(self):
        """
        Process all messages that are marked for retry and are due to be sent
        This can be called by external schedulers to handle retry messages
        
        Returns:
            int: Number of retry messages processed
        """
        # Find all retry messages that are due
        retry_messages = BulkCampaignMessage.objects.filter(
            status='retry',
            scheduled_for__lte=timezone.now(),
            campaign__active=True,
            campaign__status__in=['active', 'scheduled']
        ).select_related(
            'campaign',
            'campaign__blast_schedule',
            'campaign__blast_schedule__short_link',
            'participant',
            'participant__lead',
            'participant__nurturing_campaign',
            'participant__nurturing_campaign__media_campaign',
            'participant__originating_subscription',
            'participant__originating_subscription__media_campaign',
            'participant__media_campaign',
            'campaign__media_campaign',
            'participant__originating_subscription__opt_in_rule',
            'participant__originating_subscription__opt_in_rule__short_link',
            'participant__originating_subscription__opt_in_rule__keyword',
            'message_group',
            'drip_message_step',
            'drip_message_step__short_link',
            'reminder_message',
            'reminder_message__short_link',
        ).order_by('scheduled_for')

        processed_count = 0

        for message in retry_messages:
            try:
                # Check if the campaign is still active
                if not message.campaign.is_active_or_scheduled():
                    logger.warning(f"Skipping retry message {message.id} from inactive campaign {message.campaign.id}")
                    continue

                # Attempt to send the retry message
                outcome = self._send_message(message)
                if outcome is SendOutcome.SENT:
                    processed_count += 1
                    logger.info(f"Successfully processed retry message {message.id} (attempt {message.retry_count})")
                elif outcome is SendOutcome.DEFERRED:
                    # Nothing was attempted, so nothing failed. Spending a retry here is
                    # what let three send-cap deferrals kill a message that was only ever
                    # waiting for capacity. Leave it for the next sweep.
                    logger.info(
                        'retry message %s deferred, not counting a retry (status=%s next_eligible_at=%s)',
                        message.id,
                        message.status,
                        message.next_eligible_at,
                    )
                else:
                    # Message failed again, handle retry logic
                    if self._handle_failed_message_retry(message):
                        logger.info(f"Retry message {message.id} marked for next retry attempt")
                    else:
                        logger.info(f"Retry message {message.id} max retries exceeded, marking as failed_final")
                        last_error = (message.metadata or {}).get('last_send_error')
                        reason = f'Max retries exceeded after {message.retry_count} failed send attempts'
                        if last_error:
                            reason = f'{reason}; last error: {last_error}'
                        message.update_status(
                            'failed_final',
                            {'error': reason},
                            error_message=reason,
                        )

            except Exception as e:
                logger.exception(f"Error processing retry message {message.id}: {e}")
                # Mark as failed if we can't process it
                message.update_status('failed', {'error': f'Processing error: {str(e)}'})

        return processed_count

    def _handle_failed_message_retry(self, message):
        """
        Handle retry logic for a failed message
        
        Args:
            message: BulkCampaignMessage instance that failed
            
        Returns:
            bool: True if message was marked for retry, False if max retries exceeded
        """
        try:
            # Check if message can be retried
            if not message.can_retry():
                logger.debug(f"Message {message.id} cannot be retried - max retries exceeded or invalid status")
                return False
            
            # Check if there's already a retry message for this same message configuration
            existing_retry = BulkCampaignMessage.check_existing_retry_message(
                participant=message.participant,
                campaign=message.campaign,
                message_type=message.message_type,
                drip_message_step=message.drip_message_step,
                reminder_message=message.reminder_message
            )
            
            if existing_retry:
                logger.warning(f"Retry message already exists for message {message.id} - skipping retry creation")
                return False
            
            # Mark message for retry
            if message.mark_for_retry():
                # Calculate retry delay
                delay_minutes = message.get_retry_delay_minutes()
                
                # Calculate next retry time
                next_retry_time = timezone.now() + timedelta(minutes=delay_minutes)
                
                # Update scheduled_for to the retry time
                message.scheduled_for = next_retry_time
                message.save()
                
                logger.info(f"Message {message.id} marked for retry in {delay_minutes} minutes (attempt {message.retry_count})")
                return True
            else:
                logger.warning(f"Failed to mark message {message.id} for retry")
                return False
                
        except Exception as e:
            logger.exception(f"Error handling retry for message {message.id}: {e}")
            return False

    def _process_drip_campaign(self, campaign):
        """Process a drip campaign"""
        if not hasattr(campaign, 'drip_schedule') or not campaign.drip_schedule:
            logger.error(f"Drip campaign {campaign.id} has no schedule")
            return 0

        schedule = campaign.drip_schedule
        now = timezone.now()

        # Find active participants that need messages
        participants = LeadNurturingParticipant.objects.filter(
            nurturing_campaign=campaign,
            status='active'
        ).select_related('lead')

        scheduled_count = 0

        for participant in participants:
            try:
                # Get or create progress
                progress = participant.drip_campaign_progress.first()
                
                # If no progress exists, we should start with the first step
                if not progress:
                    first_step = schedule.message_steps.order_by('order').first()
                    if not first_step:
                        logger.error(f"No message steps found for drip schedule {schedule.id}")
                        continue
                    
                    progress = DripCampaignProgress.objects.create(
                        participant=participant,
                        current_step=first_step,
                        next_scheduled_interval=now
                    )
                
                # If no current step, we're done with the sequence
                if not progress.current_step:
                    continue
                
                # Schedule next message if needed
                if self._schedule_drip_message(participant, schedule):
                    scheduled_count += 1
                
                    # Schedule initial opt-out notice after regular message if needed
                    self._schedule_initial_opt_out_notice(participant)

            except Exception as e:
                logger.exception(f"Error processing participant {participant.id}: {str(e)}")
                continue

        return scheduled_count

    def _process_reminder_campaign(self, campaign):
        """Process a reminder campaign"""
        if not hasattr(campaign, 'reminder_schedule') or not campaign.reminder_schedule:
            logger.error(f"Reminder campaign {campaign.id} has no schedule")
            return 0

        schedule = campaign.reminder_schedule
        now = timezone.now()

        # Find active participants that need reminders and have scheduled reachouts
        # Exclude participants that have received regular messages
        participants = LeadNurturingParticipant.objects.filter(
            nurturing_campaign=campaign,
            status='active',
            lead__scheduled_reachouts__status='open'  # Only include leads with open scheduled reachouts
        ).exclude(
            bulk_messages__campaign=campaign,
            bulk_messages__message_type='regular'  # Only exclude regular messages
        ).select_related('lead').distinct()

        scheduled_count = 0

        for participant in participants:
            # Get the scheduled reachout for this lead
            scheduled_reachout = participant.lead.scheduled_reachouts.filter(
                status='open'
            ).order_by('scheduled_date').first()

            if not scheduled_reachout:
                continue

            # Find next reminder time
            next_reminder = self._get_next_reminder_time(participant, schedule)
            if not next_reminder:
                continue

            # Schedule reminder
            if self._schedule_reminder_message(participant, next_reminder, scheduled_reachout):
                scheduled_count += 1
                # Schedule initial opt-out notice after regular message if needed
                self._schedule_initial_opt_out_notice(participant)

        return scheduled_count

    def _process_blast_campaign(self, campaign):
        """Process a blast campaign"""
        if not hasattr(campaign, 'blast_schedule') or not campaign.blast_schedule:
            logger.error(f"Blast campaign {campaign.id} has no schedule")
            return 0

        schedule = campaign.blast_schedule

        # Check if campaign is active
        if not campaign.is_active_or_scheduled():
            return 0

        # Find active participants that haven't received the blast
        # Exclude participants that have received regular messages
        participants = LeadNurturingParticipant.objects.filter(
            nurturing_campaign=campaign,
            status='active'
        ).exclude(
            bulk_messages__campaign=campaign,
            bulk_messages__message_type='regular'  # Only exclude regular messages
        ).select_related('lead')

        scheduled_count = 0

        for participant in participants:
            try:
                # Schedule blast message
                if self._schedule_blast_message(participant, schedule):
                    scheduled_count += 1
                    
                    # Schedule initial opt-out notice after regular message if needed
                    self._schedule_initial_opt_out_notice(participant)

            except Exception as e:
                logger.exception(f"Error processing participant {participant.id}: {e}")
                continue

        return scheduled_count

    def _get_next_reminder_time(self, participant, schedule):
        """Get the next reminder time for a participant"""
        # Get all reminder times ordered appropriately
        reminder_times = schedule.reminder_times.all().order_by(
            'days_before', 'days_before_relative', 'hours_before', 'minutes_before'
        )

        # Get the scheduled reachout for this lead
        scheduled_reachout = participant.lead.scheduled_reachouts.filter(
            status='open'
        ).order_by('scheduled_date').first()

        if not scheduled_reachout:
            logger.warning(f"No scheduled reachout found for participant {participant.id}")
            return None

        if schedule.use_relative_schedule:
            # For relative scheduling, we need the scheduled reachout date
            appointment_time = scheduled_reachout.scheduled_date
            if not appointment_time:
                logger.warning(f"No scheduled date found for participant {participant.id}")
                return None

            # Get all sent reminders for this participant
            sent_reminders = participant.reminder_campaign_progress.all()
            
            # For relative scheduling, we need to check if we've already sent reminders
            # with the same relative timing
            for reminder in reminder_times:
                # Calculate total minutes before appointment
                total_minutes = reminder.get_total_minutes_before()
                
                # Check if we've already sent a reminder at this relative time
                already_sent = False
                for sent_reminder in sent_reminders:
                    # Calculate the time difference between the sent reminder and appointment
                    if sent_reminder.sent_at:
                        time_diff = appointment_time - sent_reminder.sent_at
                        sent_minutes = time_diff.total_seconds() / 60
                        if abs(sent_minutes - total_minutes) < 1:  # Allow 1 minute tolerance
                            already_sent = True
                            break
                
                if not already_sent:
                    return reminder

        else:
            # For absolute scheduling
            sent_days = set(
                participant.reminder_campaign_progress.values_list('days_before', flat=True)
            )

            for reminder in reminder_times:
                if reminder.days_before not in sent_days:
                    return reminder

        return None

    def _schedule_drip_message(self, participant, schedule):
        """Schedule a drip campaign message"""
        try:
            with transaction.atomic():
                # Get or create progress
                progress = participant.drip_campaign_progress.first()
                if not progress:
                    # Start with the first step
                    first_step = schedule.message_steps.order_by('order').first()
                    if not first_step:
                        logger.error(f"No message steps found for drip schedule {schedule.id}")
                        return False
                    
                    progress = DripCampaignProgress.objects.create(
                        participant=participant,
                        current_step=first_step,
                        next_scheduled_interval=timezone.now()
                    )
                
                # Get the current step
                current_step = progress.current_step
                if not current_step:
                    logger.debug(f"Participant {participant.id} has no current step")
                    return False
                
                # Check if we already have a message scheduled for this step
                # Include 'failed' and 'retry' status to prevent scheduling new messages when there's already a failed/retry one
                existing_message = BulkCampaignMessage.objects.filter(
                    participant=participant,
                    campaign=participant.nurturing_campaign,
                    drip_message_step=current_step,
                    status__in=['pending', 'scheduled', 'failed', 'retry']
                ).first()
                
                if existing_message:
                    if existing_message.status in ['failed', 'retry']:
                        logger.debug(f"Message already exists with {existing_message.status} status for participant {participant.id} at step {current_step.order} - skipping new scheduling")
                    else:
                        logger.debug(f"Message already scheduled for participant {participant.id} at step {current_step.order}")
                    return False
                
                # Calculate next send time
                now = timezone.now()
                delay = current_step.get_delay_timedelta()
                next_time = now + delay
                
                # Apply schedule restrictions
                next_time = self.time_calculator.get_next_valid_time(next_time, schedule)
                
                # Update progress with next interval
                progress.next_scheduled_interval = next_time
                progress.save()
                
                # Validate message step has content through channel config
                channel_config = current_step.get_channel_config()
                if not channel_config:
                    logger.error(f"Message step {current_step.id} has no content in channel config")
                    return False

                # Create or get message group
                message_group = self.message_group.create_or_get_message_group(
                    participant.nurturing_campaign,
                    participant,
                    next_time
                )

                if not message_group:
                    logger.error(f"Failed to create/get message group for participant {participant.id}")
                    return False

                # Create regular message safely (will return existing message if one exists)
                try:
                    message = BulkCampaignMessage.create_message_safely(
                        participant=participant,
                        campaign=participant.nurturing_campaign,
                        message_type='regular',
                        status='scheduled',
                        scheduled_for=next_time,
                        drip_message_step=current_step,
                        step_order=current_step.order,
                        message_group=message_group
                    )
                    
                    # If message already existed, don't schedule another one
                    if message.created_at < now - timedelta(seconds=5):  # Allow 5 second tolerance for race conditions
                        logger.info(f"Message already exists for participant {participant.id} at step {current_step.order}")
                        return False
                        
                except Exception as e:
                    logger.error(f"Failed to create message for participant {participant.id}: {str(e)}")
                    return False

                # Schedule opt-out notice if needed
                if not participant.opt_out_message_sent and participant.nurturing_campaign.enable_opt_out:
                    try:
                        # Schedule opt-out notice after regular message
                        opt_out_message = BulkCampaignMessage.create_message_safely(
                            participant=participant,
                            campaign=participant.nurturing_campaign,
                            message_type='opt_out_notice',
                            status='scheduled',
                            scheduled_for=next_time + timedelta(minutes=1),  # Send 1 minute after regular message
                            drip_message_step=current_step,  # Add the missing drip_message_step parameter
                            message_group=message_group
                        )
                        participant.opt_out_message_sent = True
                        participant.save()
                    except Exception as e:
                        # If opt-out message fails, cancel the group
                        self.message_group.cancel_group(
                            message_group,
                            f"Failed to schedule opt-out message: {str(e)}"
                        )
                        logger.error(f"Failed to schedule opt-out message for participant {participant.id}: {str(e)}")
                        return False
                
                # Update participant progress
                try:
                    participant.update_campaign_progress(
                        scheduled_time=next_time
                    )
                except Exception as e:
                    # If progress update fails, cancel the group
                    self.message_group.cancel_group(
                        message_group,
                        f"Failed to update participant progress: {str(e)}"
                    )
                    logger.error(f"Failed to update participant progress for {participant.id}: {str(e)}")
                    return False
                
                return True
                
        except Exception as e:
            logger.exception(f"Error scheduling drip message for participant {participant.id}: {str(e)}")
            return False

    def _schedule_reminder_message(self, participant, reminder, scheduled_reachout):
        """Schedule a reminder campaign message"""
        try:
            with transaction.atomic():
                # Calculate send time based on reminder settings and scheduled reachout date
                send_time = self.time_calculator.calculate_reminder_time(
                    reminder,
                    scheduled_reachout.scheduled_date,
                    reminder.schedule.use_relative_schedule
                )

                if not send_time:
                    logger.debug(f"No valid send time calculated for participant {participant.id}")
                    return False

                campaign = participant.nurturing_campaign

                # Find the correct ReminderMessage for the reminder time and campaign channel
                reminder_message = None
                if campaign.channel == 'sms':
                    reminder_message = reminder.messages.filter(sms_config__isnull=False).first()
                elif campaign.channel == 'email':
                    reminder_message = reminder.messages.filter(email_config__isnull=False).first()
                elif campaign.channel == 'voice':
                    reminder_message = reminder.messages.filter(voice_config__isnull=False).first()
                elif campaign.channel == 'chat':
                    reminder_message = reminder.messages.filter(chat_config__isnull=False).first()

                if not reminder_message:
                    logger.error(f"No ReminderMessage found for reminder {reminder.id} and channel {campaign.channel}")
                    return False

                # Create or get message group
                message_group = self.message_group.create_or_get_message_group(
                    participant.nurturing_campaign,
                    participant,
                    send_time
                )

                if not message_group:
                    logger.error(f"Failed to create/get message group for participant {participant.id}")
                    return False

                # Create message safely (will return existing message if one exists)
                message = BulkCampaignMessage.create_message_safely(
                    participant=participant,
                    campaign=participant.nurturing_campaign,
                    message_type='regular',
                    status='scheduled',
                    scheduled_for=send_time,
                    reminder_message=reminder_message,
                    message_group=message_group
                )
                
                # If message already existed, don't schedule another one
                now = timezone.now()
                if message.created_at < now - timedelta(seconds=5):  # Allow 5 second tolerance for race conditions
                    logger.info(f"Reminder message already exists for participant {participant.id} and reminder {reminder.id}")
                    return False

                # Create progress record
                if reminder.schedule.use_relative_schedule:
                    # For relative scheduling, store all the relative timing fields
                    ReminderCampaignProgress.objects.create(
                        participant=participant,
                        days_before_relative=reminder.days_before_relative,
                        hours_before=reminder.hours_before,
                        minutes_before=reminder.minutes_before,
                        sent_at=send_time,
                        reminder_time=reminder
                    )
                else:
                    # For absolute scheduling
                    ReminderCampaignProgress.objects.create(
                        participant=participant,
                        days_before=reminder.days_before,
                        time=reminder.time,
                        sent_at=send_time,
                        reminder_time=reminder
                    )

                # Update participant progress
                participant.update_campaign_progress(
                    scheduled_time=message.scheduled_for
                )

                return True
        except Exception as e:
            logger.exception(f"Error scheduling reminder message: {e}")
            return False

    def _schedule_blast_message(self, participant, schedule):
        """Schedule a blast campaign message"""
        try:
            with transaction.atomic():
                # Check if we already have a message scheduled for this participant
                # Include 'failed' and 'retry' status to prevent scheduling new messages when there's already a failed/retry one
                existing_message = BulkCampaignMessage.objects.filter(
                    participant=participant,
                    campaign=participant.nurturing_campaign,
                    message_type='regular',
                    status__in=['pending', 'scheduled', 'failed', 'retry']
                ).first()
                
                if existing_message:
                    if existing_message.status in ['failed', 'retry']:
                        logger.debug(f"Blast message already exists with {existing_message.status} status for participant {participant.id} - skipping new scheduling")
                    else:
                        logger.debug(f"Blast message already scheduled for participant {participant.id}")
                    return False

                # Start with the original send time
                send_time = schedule.send_time
                
                # Apply business hours adjustment if enabled
                if schedule.business_hours_only:
                    send_time = self.time_calculator.get_next_valid_time(send_time, schedule)

                # Create or get message group
                message_group = self.message_group.create_or_get_message_group(
                    participant.nurturing_campaign,
                    participant,
                    send_time
                )

                if not message_group:
                    logger.error(f"Failed to create/get message group for participant {participant.id}")
                    return False

                # Create message safely (will return existing message if one exists)
                message = BulkCampaignMessage.create_message_safely(
                    participant=participant,
                    campaign=participant.nurturing_campaign,
                    message_type='regular',
                    status='scheduled',
                    scheduled_for=send_time,
                    message_group=message_group
                )
                
                # If message already existed, don't schedule another one
                now = timezone.now()
                if message.created_at < now - timedelta(seconds=5):  # Allow 5 second tolerance for race conditions
                    logger.info(f"Blast message already exists for participant {participant.id}")
                    return False

                # Create or update blast progress
                progress, created = BlastCampaignProgress.objects.get_or_create(
                    participant=participant,
                    defaults={
                        'message_sent': False,
                        'sent_at': None
                    }
                )

                # Update participant progress
                participant.update_campaign_progress(
                    scheduled_time=message.scheduled_for
                )

                logger.info(f"Scheduled blast message for participant {participant.id} at {send_time}")
                return True

        except Exception as e:
            logger.exception(f"Error scheduling blast message for participant {participant.id}: {e}")
            return False

    def _send_message(self, message):
        """Send a scheduled message"""
        claim = None
        try:
            # Get campaign and participant
            campaign = message.campaign
            participant = message.participant

            # Check if message can be sent (including retry status)
            if not message.can_be_sent() and message.status != 'retry':
                logger.debug(f"Cannot send message {message.id} - status: {message.status}")
                return SendOutcome.DEFERRED

            # For retry messages, check if it's time to retry
            if message.status == 'retry':
                if message.scheduled_for and message.scheduled_for > timezone.now():
                    logger.debug(f"Retry message {message.id} not yet due - scheduled for {message.scheduled_for}")
                    return SendOutcome.DEFERRED

            # Check if message can be sent
            if not campaign.can_send_message(participant):
                logger.debug(f"Cannot send message {message.id} - campaign or participant not active")
                return SendOutcome.DEFERRED

            # Check business hours and weekend restrictions before sending for all campaign types
            # that have business_hours_only enabled
            if campaign.crm_campaign and hasattr(campaign, 'drip_schedule') and campaign.drip_schedule and campaign.drip_schedule.business_hours_only:
                if not self.time_calculator.is_within_campaign_operating_hours(timezone.now(), campaign.crm_campaign):
                    logger.debug(f"Cannot send drip message {message.id} - outside campaign operating hours")
                    return SendOutcome.DEFERRED
            elif campaign.crm_campaign and hasattr(campaign, 'reminder_schedule') and campaign.reminder_schedule and campaign.reminder_schedule.business_hours_only:
                if not self.time_calculator.is_within_campaign_operating_hours(timezone.now(), campaign.crm_campaign):
                    logger.debug(f"Cannot send reminder message {message.id} - outside campaign operating hours")
                    return SendOutcome.DEFERRED
            elif campaign.crm_campaign and hasattr(campaign, 'blast_schedule') and campaign.blast_schedule and campaign.blast_schedule.business_hours_only:
                if not self.time_calculator.is_within_campaign_operating_hours(timezone.now(), campaign.crm_campaign):
                    logger.debug(f"Cannot send blast message {message.id} - outside campaign operating hours")
                    return SendOutcome.DEFERRED

            # For blast: use message.scheduled_for as source of truth (already used for "due" query).
            # Avoids first-send failure when schedule.send_time and message.scheduled_for differ (e.g. timezone).
            if campaign.campaign_type == 'blast' and message.scheduled_for:
                now = timezone.now()
                if now < message.scheduled_for:
                    logger.debug(f"Cannot send blast message {message.id} - scheduled_for {message.scheduled_for} not reached yet")
                    return SendOutcome.DEFERRED

            # Email: replay-safe skip when a worker retries after Mailgun already accepted the send.
            email_send_idempotency_key = None
            if campaign.channel == 'email':
                message.refresh_from_db()
                email_send_idempotency_key = (message.metadata or {}).get(
                    'send_idempotency_key'
                ) or f'bulk_campaign_message:{message.id}'
                if message.status == 'sent' and (message.provider_message_id or '').strip():
                    logger.info(
                        'bulk_email_idempotent_skip bulk_campaign_message_id=%s '
                        'nurturing_campaign_id=%s send_idempotency_key=%s provider_message_id=%s',
                        message.id,
                        campaign.id,
                        email_send_idempotency_key,
                        message.provider_message_id,
                    )
                    return SendOutcome.SENT

            # Get message content (with optional short link and keyword for drip/reminder/blast)
            extra_context = None
            link, drip_step_id, reminder_message_id, blast_schedule_id, keyword_str = self._resolve_link_for_bulk_message(message, campaign)
            if link is not None:
                acs_context = {
                    'lead': message.participant.lead,
                    'campaign': campaign,
                }
                # Resolve service_phone for utm_context short_code (from number)
                service_phone_for_link = None
                if campaign.channel in ['sms', 'voice']:
                    ch_config = None
                    if campaign.campaign_type == 'drip' and message.drip_message_step:
                        ch_config = message.drip_message_step.get_channel_config()
                    elif campaign.campaign_type == 'reminder' and message.reminder_message:
                        ch_config = message.reminder_message.get_channel_config()
                    elif campaign.campaign_type == 'blast':
                        ch_config = self._get_campaign_channel_config(campaign)
                    if ch_config and hasattr(ch_config, 'get_from_number'):
                        service_phone_for_link = ch_config.get_from_number()
                utm_context = {
                    'keyword': keyword_str or '',
                    'short_code': service_phone_for_link or '',
                }
                if not ensure_link_published(link, utm_context=utm_context, acs_context=acs_context):
                    if drip_step_id is not None:
                        logger.error("Aborting send: failed to publish short link for drip step %s", drip_step_id)
                    elif reminder_message_id is not None:
                        logger.error("Aborting send: failed to publish short link for reminder message %s", reminder_message_id)
                    elif blast_schedule_id is not None:
                        logger.error("Aborting send: failed to publish short link for blast schedule %s", blast_schedule_id)
                    else:
                        logger.error("Aborting send: failed to publish short link for blast message %s", message.id)
                    return SendOutcome.FAILED
                resolved_url = build_bulk_short_url(
                    link,
                    drip_step_id=drip_step_id,
                    reminder_message_id=reminder_message_id,
                    blast_schedule_id=blast_schedule_id,
                )
                # Set link and keyword so {{link.short_link}} and {{keyword.keyword}} resolve
                extra_context = {
                    'link': {'short_link': resolved_url},
                    'Link': {'short_link': resolved_url},
                    'keyword': {'keyword': keyword_str},
                    'Keyword': {'keyword': keyword_str},
                }
            else:
                # Still pass keyword for body {{keyword.keyword}} when no link resolved
                if keyword_str:
                    extra_context = {
                        'keyword': {'keyword': keyword_str},
                        'Keyword': {'keyword': keyword_str},
                    }
                # Log when content contains {{link.short_link}} but no link could be resolved
                if campaign.campaign_type in ('drip', 'reminder', 'blast'):
                    channel_config = None
                    if campaign.campaign_type == 'drip' and message.drip_message_step:
                        channel_config = message.drip_message_step.get_channel_config()
                    elif campaign.campaign_type == 'reminder' and message.reminder_message:
                        channel_config = message.reminder_message.get_channel_config()
                    elif campaign.campaign_type == 'blast':
                        channel_config = self._get_campaign_channel_config(campaign)
                    raw_content = (channel_config.template.content if channel_config and getattr(channel_config, 'template', None) else None) or (channel_config.content if channel_config and getattr(channel_config, 'content', None) else '') or ''
                    if raw_content and ('{{link.short_link}}' in raw_content or '{{Link.short_link}}' in raw_content):
                        logger.warning(
                            "%s %s has no short link resolved but message content contains {{link.short_link}}. "
                            "Assign a Short Link or enable use_opt_in_rule_link with a valid opt-in rule link.",
                            "Drip step" if campaign.campaign_type == 'drip' else ("Reminder message" if campaign.campaign_type == 'reminder' else "Blast"),
                            message.drip_message_step.id if campaign.campaign_type == 'drip' else (message.reminder_message.id if campaign.campaign_type == 'reminder' else message.id),
                        )
            processed_content = message.get_message_content(extra_context=extra_context)

            # Get service phone number for SMS/Voice using modular channel configuration
            service_phone = None
            if campaign.channel in ['sms', 'voice']:
                if message.message_type in ['opt_out_notice', 'opt_out_confirmation']:
                    if campaign.campaign_type == 'drip' and hasattr(campaign, 'drip_schedule') and campaign.drip_schedule:
                        first_step = campaign.drip_schedule.message_steps.order_by('order').first()
                        if first_step:
                            channel_config = first_step.get_channel_config()
                            if channel_config and hasattr(channel_config, 'get_from_number'):
                                service_phone = channel_config.get_from_number()
                    elif campaign.campaign_type == 'reminder':
                        # Get the first reminder_message in the message group
                        first_reminder_message = None
                        if message.message_group:
                            first_reminder_message = (
                                BulkCampaignMessage.objects
                                .filter(message_group=message.message_group, reminder_message__isnull=False)
                                .order_by('scheduled_for')
                                .first()
                            )
                        if first_reminder_message and first_reminder_message.reminder_message:
                            channel_config = first_reminder_message.reminder_message.get_channel_config()
                            if channel_config and hasattr(channel_config, 'get_from_number'):
                                service_phone = channel_config.get_from_number()
                    else:
                        # For blast campaigns, use campaign-level config
                        channel_config = self._get_campaign_channel_config(campaign)
                        if channel_config and hasattr(channel_config, 'get_from_number'):
                            service_phone = channel_config.get_from_number()
                elif campaign.campaign_type == 'drip' and message.drip_message_step:
                    channel_config = message.drip_message_step.get_channel_config()
                    if channel_config and hasattr(channel_config, 'get_from_number'):
                        service_phone = channel_config.get_from_number()
                elif campaign.campaign_type == 'reminder' and message.reminder_message:
                    channel_config = message.reminder_message.get_channel_config()
                    if channel_config and hasattr(channel_config, 'get_from_number'):
                        service_phone = channel_config.get_from_number()
                else:
                    # For blast campaigns, use campaign-level config
                    channel_config = self._get_campaign_channel_config(campaign)
                    if channel_config and hasattr(channel_config, 'get_from_number'):
                        service_phone = channel_config.get_from_number()

            # For opt-out messages, we want to send immediately
            if message.message_type in ['opt_out_notice', 'opt_out_confirmation']:
                message.scheduled_for = timezone.now()

            # Get channel configuration for the message
            channel_config = None
            if campaign.campaign_type == 'drip' and message.drip_message_step:
                channel_config = message.drip_message_step.get_channel_config()
            elif campaign.campaign_type == 'reminder' and message.reminder_message:
                channel_config = message.reminder_message.get_channel_config()
            else:
                channel_config = self._get_campaign_channel_config(campaign)

            email_context = None
            if campaign.channel == 'email':
                email_context = build_nested_template_context(
                    lead=participant.lead,
                    nurturing_campaign=campaign,
                    sender_user=campaign.created_by,
                )

            log_context = None
            if campaign.channel == 'email' and email_send_idempotency_key is not None:
                log_context = {
                    'bulk_campaign_message_id': message.id,
                    'nurturing_campaign_id': campaign.id,
                    'send_idempotency_key': email_send_idempotency_key,
                }
                if channel_config and getattr(channel_config, 'from_endpoint_id', None):
                    log_context['contact_endpoint_id'] = channel_config.from_endpoint_id

            participant_media = resolve_media_campaign_for_participant(participant)

            claim = try_claim_send_slot(
                campaign=campaign,
                channel=campaign.channel,
                message_type=message.message_type,
            )
            if not claim.allowed:
                message.scheduled_for = claim.next_reset_at
                message.next_eligible_at = claim.next_reset_at
                if claim.blocking_cap_id is not None and claim.blocking_cap_period:
                    message.deferral_reason = f'cap:{claim.blocking_cap_period}:{claim.blocking_cap_id}'
                else:
                    message.deferral_reason = ''
                deferral_note = (
                    f'Waiting on {claim.blocking_cap_period} send cap '
                    f'(cap {claim.blocking_cap_id}); eligible again '
                    f'{claim.next_reset_at.isoformat() if claim.next_reset_at else "unknown"}'
                    if claim.blocking_cap_id is not None
                    else 'Waiting on send cap'
                )
                message.update_status(
                    'scheduled',
                    {
                        'cap_deferral': {
                            'cap_id': claim.blocking_cap_id,
                            'period': claim.blocking_cap_period,
                            'next_reset_at': claim.next_reset_at.isoformat() if claim.next_reset_at else None,
                            'note': deferral_note,
                        },
                    },
                    # Not an error: 'scheduled' would clear this anyway, but say so
                    # explicitly since the note is the useful bit.
                    error_message='',
                )
                logger.info(
                    'send_cap_deferred bulk_campaign_message_id=%s campaign_id=%s cap_id=%s period=%s next_reset_at=%s',
                    message.id,
                    campaign.id,
                    claim.blocking_cap_id,
                    claim.blocking_cap_period,
                    claim.next_reset_at,
                )
                return SendOutcome.DEFERRED

            if claim.claim_token and claim.bucket_ids:
                message.update_status(
                    message.status,
                    {
                        'send_cap_claim': {
                            'claim_token': claim.claim_token,
                            'bucket_ids': list(claim.bucket_ids),
                            'claimed_at': claim.claimed_at.isoformat() if claim.claimed_at else None,
                        },
                    },
                )
                logger.info(
                    'send_cap_claim_granted bulk_campaign_message_id=%s campaign_id=%s token=%s bucket_ids=%s',
                    message.id,
                    campaign.id,
                    claim.claim_token,
                    list(claim.bucket_ids),
                )

            # Send message using the delivery service
            success, thread_message = self.message_delivery.send_message(
                channel=campaign.channel,
                content=processed_content,
                lead=participant.lead,
                user=campaign.created_by,
                subject=campaign.subject if hasattr(campaign, 'subject') else None,
                service_phone=service_phone,
                message_type=message.message_type,  # Pass message type to delivery service
                channel_config=channel_config,  # Pass channel configuration to delivery service
                email_context=email_context,
                log_context=log_context,
                media_campaign=participant_media,
            )

            if success:
                if claim and claim.claim_token and claim.bucket_ids:
                    message.metadata = clear_send_cap_claim_metadata(message.metadata or {})
                    message.save(update_fields=['metadata', 'updated_at'])
                # Update message status (persist idempotency key for email bulk sends)
                if campaign.channel == 'email' and email_send_idempotency_key:
                    message.update_status('sent', {'send_idempotency_key': email_send_idempotency_key})
                else:
                    message.update_status('sent')

                # Persist Twilio SID on BulkCampaignMessage for reply tracking (ParentMessageSid lookup)
                if campaign.channel == 'sms' and thread_message:
                    twilio_msg = getattr(thread_message, 'twilio_message', None)
                    twilio_sid = getattr(twilio_msg, 'message_sid', None) if twilio_msg else None
                    if twilio_sid:
                        message.provider_message_id = twilio_sid
                        message.save(update_fields=['provider_message_id'])
                elif campaign.channel == 'email' and thread_message:
                    mailgun_mid = getattr(thread_message, 'email_provider_message_id', None)
                    if mailgun_mid:
                        message.provider_message_id = mailgun_mid
                        message.save(update_fields=['provider_message_id'])
                
                # Update participant progress
                participant.update_campaign_progress(message_sent=True)

                # For drip campaigns, update the current step if this was the last message in the sequence
                if campaign.campaign_type == 'drip' and message.drip_message_step:
                    progress = participant.drip_campaign_progress.first()
                    if progress and progress.current_step == message.drip_message_step:
                        # Find next step
                        if hasattr(campaign, 'drip_schedule') and campaign.drip_schedule:
                            next_step = campaign.drip_schedule.message_steps.filter(
                                order__gt=message.drip_message_step.order
                            ).order_by('order').first()
                        else:
                            next_step = None
                        
                        if next_step:
                            progress.current_step = next_step
                        else:
                            progress.current_step = None
                        progress.save()

                logger.info(f"Successfully sent message {message.id} (retry attempt: {message.retry_count})")
                return SendOutcome.SENT

            # Message failed to send
            logger.warning(f"Failed to send message {message.id} (retry attempt: {message.retry_count})")
            # MessageDeliveryService.send_message returns only (success, thread_message),
            # so the provider's reason is not available here -- record what is known so a
            # later failed_final can quote something more useful than a generic string.
            message.update_status(
                message.status,
                {'last_send_error': f'{campaign.channel} delivery reported failure'},
            )
            if claim and claim.bucket_ids and should_refund_after_send_failure(success, thread_message):
                refund_send_slot(claim=claim)
                logger.info(
                    'send_cap_refunded bulk_campaign_message_id=%s token=%s bucket_ids=%s reason=send_failure',
                    message.id,
                    getattr(claim, 'claim_token', None),
                    list(claim.bucket_ids),
                )
            return SendOutcome.FAILED

        except Exception as e:
            if claim and getattr(claim, 'bucket_ids', ()):
                try:
                    refund_send_slot(claim=claim)
                    logger.info(
                        'send_cap_refunded bulk_campaign_message_id=%s token=%s bucket_ids=%s reason=exception',
                        message.id,
                        getattr(claim, 'claim_token', None),
                        list(claim.bucket_ids),
                    )
                except Exception:
                    logger.exception('send_cap_refund_after_exception_failed bulk_campaign_message_id=%s', message.id)
            logger.exception(f"Error sending message {message.id}: {e}")
            message.update_status(
                'failed',
                {'error': str(e), 'last_send_error': str(e)},
                error_message=str(e),
            )
            return SendOutcome.FAILED

    def _get_next_send_time(self, schedule):
        """Calculate the next time a message should be sent based on schedule"""
        now = timezone.now()
        
        # If outside business hours, move to next business day
        if schedule.business_hours_only:
            if now.time() >= schedule.end_time:
                # Move to next day
                next_day = now + timezone.timedelta(days=1)
                if schedule.exclude_weekends and next_day.weekday() >= 5:
                    # Skip weekend
                    next_day += timezone.timedelta(days=2)
                return timezone.make_aware(timezone.datetime.combine(next_day.date(), schedule.start_time))
            elif now.time() < schedule.start_time:
                # Move to start time today
                return timezone.make_aware(timezone.datetime.combine(now.date(), schedule.start_time))

        return now

    def _calculate_reminder_time(self, reminder, participant=None, scheduled_reachout_date=None):
        """Calculate the send time for a reminder"""
        now = timezone.now()

        if reminder.schedule.use_relative_schedule:
            if not scheduled_reachout_date:
                logger.warning("No scheduled reachout date provided for relative scheduling")
                return None

            total_minutes = reminder.get_total_minutes_before()
            
            # Calculate time before scheduled reachout
            send_time = scheduled_reachout_date - timezone.timedelta(minutes=total_minutes)
            
            # If the calculated time is in the past, return None
            if send_time <= now:
                logger.debug(f"Calculated send time {send_time} is in the past")
                return None
                
            return send_time
        else:
            # Absolute scheduling
            if reminder.days_before is not None:
                if not scheduled_reachout_date:
                    logger.warning("No scheduled reachout date provided for absolute scheduling")
                    return None

                send_date = scheduled_reachout_date.date() - timezone.timedelta(days=reminder.days_before)
                if reminder.time:
                    return timezone.make_aware(timezone.datetime.combine(send_date, reminder.time))
                return timezone.make_aware(timezone.datetime.combine(send_date, time(9, 0)))  # Default to 9 AM

        return None

    def _add_identity_participant(self, conversation_obj, identity, projected_address=None):
        """
        Add an identity-based participant to the conversation.
        
        Args:
            conversation_obj: The conversation object
            identity: The identity to use for the participant (e.g., 'acs-system')
            projected_address: The phone number to project for this identity (e.g., Twilio number)
            
        Returns:
            tuple: (participant_obj, created) where created is a boolean indicating if the participant was newly created
        """
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        
        # First check if the participant exists in our database
        existing = Participant.objects.filter(
            conversation=conversation_obj,
            user=None,  # If you don't link a user in your DB for 'system'
            phone_number=None,
        ).first()

        if existing:
            logger.debug(f"Found existing participant in database with SID: {existing.participant_sid}")
            return existing, False

        # Check if the participant exists in Twilio
        logger.debug(f"Checking for existing participant with identity='{identity}' in Twilio")
        existing_participants = client.conversations \
            .conversations(conversation_obj.twilio_sid) \
            .participants \
            .list()

        # Look for a participant with matching identity
        for participant in existing_participants:
            if participant.identity == identity:
                logger.debug(f"Found existing participant in Twilio with SID: {participant.sid}")
                # Create or update our database record
                participant_obj, _ = Participant.objects.get_or_create(
                    participant_sid=participant.sid,
                    defaults={
                        'conversation': conversation_obj,
                        'phone_number': None,
                        'user': None
                    }
                )
                return participant_obj, False

        # If we get here, we need to create a new participant
        logger.debug(f"Adding new identity participant with identity='{identity}'")
        participant_params = {'identity': identity}
        
        # Add projected address if provided
        if projected_address:
            logger.debug(f"Using projected address: {projected_address}")
            participant_params['messaging_binding_projected_address'] = projected_address

        participant = client.conversations \
            .conversations(conversation_obj.twilio_sid) \
            .participants \
            .create(**participant_params)

        participant_obj = Participant.objects.create(
            participant_sid=participant.sid,
            conversation=conversation_obj,
            phone_number=None,
            user=None  # or link to a special "system" user if desired
        )
        return participant_obj, True

    def _get_campaign_channel_config(self, campaign):
        """
        Get the appropriate channel configuration for a campaign based on its channel type.
        
        Args:
            campaign: The campaign object
            
        Returns:
            The channel configuration object (EmailConfig, SMSConfig, VoiceConfig, or ChatConfig)
        """
        if campaign.channel == 'email':
            return campaign.email_config
        elif campaign.channel == 'sms':
            return campaign.sms_config
        elif campaign.channel == 'voice':
            return campaign.voice_config
        elif campaign.channel == 'chat':
            return campaign.chat_config
        return None

    def _format_phone_number(self, phone_number):
        """
        Format phone number to E.164 format required by Twilio, or return as-is for short codes.
        Args:
            phone_number (str): Raw phone number in any format (e.g., XXX-XXX-XXXX, (XXX) XXX-XXXX, etc.)
        Returns:
            str: Phone number in E.164 format, or short code digits as-is
        """
        if not phone_number:
            logger.debug("No phone number provided to format")
            return None

        # Remove any non-digit characters (including hyphens, parentheses, spaces)
        digits = ''.join(filter(str.isdigit, phone_number))

        # Short codes (typically 4-6 digits) - return as-is; Twilio accepts them without country code
        if 4 <= len(digits) <= 6:
            return digits

        # Handle XXX-XXX-XXXX format (10 digits)
        if len(digits) == 10:
            formatted = f"+1{digits}"
            return formatted

        # If number starts with 1 and is 11 digits, it's already a US number
        if len(digits) == 11 and digits.startswith('1'):
            formatted = f"+{digits}"
            return formatted

        # If number already has country code (starts with +), just ensure it's clean
        if phone_number.startswith('+'):
            formatted = f"+{digits}"
            return formatted

        # If we can't determine the format, return None
        logger.warning(f"Could not determine format for phone number: {phone_number}")
        return None

    def schedule_opt_out_message(self, participant, message_type='opt_out_confirmation'):
        """
        Schedule an opt-out message for a participant
        
        Args:
            participant: LeadNurturingParticipant instance
            message_type: Type of opt-out message ('opt_out_notice' or 'opt_out_confirmation')
            
        Returns:
            bool: True if message was scheduled successfully
        """
        try:
            with transaction.atomic():
                campaign = participant.nurturing_campaign
                
                # For opt-out confirmations, we can send immediately
                if message_type == 'opt_out_confirmation':
                    scheduled_for = timezone.now()
                else:
                    # For opt-out notices, we need to check if there are any pending regular messages
                    # Include 'failed' status to prevent scheduling new opt-out messages when there's already a failed one
                    pending_regular = BulkCampaignMessage.objects.filter(
                        participant=participant,
                        status__in=['pending', 'scheduled', 'failed'],
                        message_type='regular'
                    ).order_by('scheduled_for').first()
                    
                    if pending_regular:
                        # Schedule opt-out notice after the last regular message
                        scheduled_for = pending_regular.scheduled_for + timedelta(minutes=1)
                    else:
                        # No pending regular messages, send immediately
                        scheduled_for = timezone.now()

                # Create or get message group
                message_group = self.message_group.create_or_get_message_group(
                    campaign,
                    participant,
                    scheduled_for
                )

                if not message_group:
                    logger.error(f"Failed to create/get message group for participant {participant.id}")
                    return False
                
                # Determine campaign-specific parameters
                drip_message_step = None
                reminder_message = None
                
                if campaign.campaign_type == 'drip':
                    # For drip campaigns, we need to get the current step
                    progress = participant.drip_campaign_progress.first()
                    if progress and progress.current_step:
                        drip_message_step = progress.current_step
                    else:
                        # If no current step, get the first step from the schedule
                        if hasattr(campaign, 'drip_schedule') and campaign.drip_schedule:
                            drip_message_step = campaign.drip_schedule.message_steps.order_by('order').first()
                elif campaign.campaign_type == 'reminder':
                    # For reminder campaigns, we need to find the appropriate reminder message
                    # Get the most recent regular message to find the associated reminder_message
                    recent_regular_message = BulkCampaignMessage.objects.filter(
                        participant=participant,
                        campaign=campaign,
                        message_type='regular',
                        reminder_message__isnull=False
                    ).order_by('-created_at').first()
                    
                    if recent_regular_message and recent_regular_message.reminder_message:
                        reminder_message = recent_regular_message.reminder_message
                    else:
                        # If no recent message found, try to get the first reminder message from the schedule
                        if hasattr(campaign, 'reminder_schedule') and campaign.reminder_schedule:
                            first_reminder_time = campaign.reminder_schedule.reminder_times.order_by(
                                'days_before', 'days_before_relative', 'hours_before', 'minutes_before'
                            ).first()
                            if first_reminder_time:
                                # Find the appropriate reminder message for the campaign channel
                                if campaign.channel == 'sms':
                                    reminder_message = first_reminder_time.messages.filter(sms_config__isnull=False).first()
                                elif campaign.channel == 'email':
                                    reminder_message = first_reminder_time.messages.filter(email_config__isnull=False).first()
                                elif campaign.channel == 'voice':
                                    reminder_message = first_reminder_time.messages.filter(voice_config__isnull=False).first()
                                elif campaign.channel == 'chat':
                                    reminder_message = first_reminder_time.messages.filter(chat_config__isnull=False).first()

                # Create message safely with campaign-specific parameters
                message = BulkCampaignMessage.create_message_safely(
                    participant=participant,
                    campaign=campaign,
                    message_type=message_type,
                    status='scheduled',
                    scheduled_for=scheduled_for,
                    drip_message_step=drip_message_step,
                    reminder_message=reminder_message,
                    message_group=message_group
                )
                
                # Update participant progress
                participant.update_campaign_progress(
                    scheduled_time=message.scheduled_for
                )
                
                return True
                
        except Exception as e:
            logger.exception(f"Error scheduling opt-out message for participant {participant.id}: {e}")
            # If we created a message group, cancel it
            if 'message_group' in locals():
                self.message_group.cancel_group(
                    message_group,
                    f"Failed to schedule opt-out message: {str(e)}"
                )
            return False

    def _schedule_initial_opt_out_notice(self, participant):
        """
        Schedule the initial opt-out notice for a participant if:
        1. The campaign has opt-out enabled
        2. The participant hasn't received the opt-out notice yet
        3. The participant is active
        """
        try:
            campaign = participant.nurturing_campaign
            
            # Check if we should schedule the opt-out notice
            if (campaign.enable_opt_out and 
                not participant.opt_out_message_sent and 
                participant.status == 'active'):
                
                # Schedule the opt-out notice
                if self.schedule_opt_out_message(participant, message_type='opt_out_notice'):
                    # Mark the opt-out message as sent
                    participant.opt_out_message_sent = True
                    participant.save()
                    logger.info(f"Scheduled initial opt-out notice for participant {participant.id}")
                    return True
                    
        except Exception as e:
            logger.exception(f"Error scheduling initial opt-out notice for participant {participant.id}: {e}")
            return False 