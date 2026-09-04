from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
import pytz
from datetime import datetime, timedelta
from .external_references import Account, Campaign, Lead
from .journeys import JourneyEvent
from .blast_campaigns import BlastCampaignProgress
from .drip_campaigns import DripCampaignProgress
from .reminder_campaigns import ReminderCampaignProgress
from .channel_configs import EmailConfig, SMSConfig, VoiceConfig, ChatConfig
from bulkcampaign_processor.utils.variable_replacement import replace_variables
# SmsMessage, SmsSubscriber: use string refs below to avoid circular import with sms_marketing.models.campaign

import logging

logger = logging.getLogger(__name__)

class LeadNurturingCampaign(models.Model):
    CAMPAIGN_TYPES = [
        ('journey', 'Journey Based'),
        ('drip', 'Drip Campaign'),
        ('reminder', 'Reminder Campaign'),
        ('blast', 'One-time Blast'),
    ]

    CHANNEL_TYPES = [
        ('email', 'Email'),
        ('sms', 'SMS'),
        ('voice', 'Voice'),
        ('chat', 'Chat'),
    ]

    CAMPAIGN_STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    # Account relationship
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='nurturing_campaigns')
    
    # Existing fields
    journey = models.ForeignKey('Journey', on_delete=models.CASCADE, related_name='nurturing_campaigns', null=True, blank=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    active = models.BooleanField(default=True)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    is_ongoing = models.BooleanField(
        default=False,
        help_text="If True, campaign runs indefinitely until manually ended"
    )
    status = models.CharField(
        max_length=20,
        choices=CAMPAIGN_STATUS_CHOICES,
        default='draft',
        help_text="Current status of the campaign"
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='status_changed_campaigns'
    )
    
    # Auto-enrollment configuration
    auto_enroll_new_leads = models.BooleanField(
        default=False,
        help_text="If True, automatically enroll new leads that match the criteria"
    )
    auto_enroll_filters = models.JSONField(
        blank=True, 
        null=True,
        help_text="""
        Filters to determine which new leads should be auto-enrolled.
        Format: [
            {
                "model": "lead|d2c_lead|b2b_lead|lead_field_value|lead_intake_value",
                "field": "field_name",
                "operator": "equals|contains|greater_than|less_than|is_empty|is_not_empty",
                "value": "value_to_compare"
            }
        ]
        """
    )
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # New fields for campaign type
    campaign_type = models.CharField(max_length=20, choices=CAMPAIGN_TYPES, default='journey')
    channel = models.CharField(
        max_length=10,
        choices=CHANNEL_TYPES,
        null=True,
        blank=True
    )
    template = models.ForeignKey(
        'MessageTemplate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bulk_campaigns'
    )
    content = models.TextField(blank=True, null=True)
    crm_campaign = models.ForeignKey(Campaign, on_delete=models.SET_NULL, null=True, blank=True, related_name='nurturing_campaigns')
    media_campaign = models.ForeignKey(
        'planning.MediaCampaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name='nurturing_campaigns',
        help_text='Default media (planning) campaign. Must belong to crm_campaign when both are set.',
    )

    # OneToOne fields for the shared channel config models
    email_config = models.OneToOneField(EmailConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    sms_config = models.OneToOneField(SMSConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    voice_config = models.OneToOneField(VoiceConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    chat_config = models.OneToOneField(ChatConfig, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # Opt-out configuration
    enable_opt_out = models.BooleanField(
        default=True,
        help_text="If True, allows participants to opt out by replying with 'STOP'"
    )
    initial_opt_out_notice = models.TextField(
        default="Reply STOP to opt out of further messages.",
        help_text="Message sent with the first message to inform participants about opt-out option"
    )
    opt_out_message = models.TextField(
        default="You have been unsubscribed from this campaign. You will no longer receive messages.",
        help_text="Message to send when a participant opts out"
    )

    # Retry configuration
    retry_strategy = models.ForeignKey(
        'RetryStrategy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Retry strategy for failed messages in this campaign"
    )
    max_retries = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override max retries for this campaign (uses retry strategy default if not set)"
    )

    class Meta:
        managed = False
        db_table = 'acs_leadnurturingcampaign'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['is_ongoing']),
            models.Index(fields=['status_changed_at']),
            models.Index(fields=['crm_campaign', 'media_campaign']),
        ]

    def clean(self):
        """Validate campaign configuration"""
        super().clean()

        # Validate end date only if not an ongoing campaign
        if not self.is_ongoing and self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("End date must be after start date")

        if self.campaign_type == 'journey':
            if not self.journey:
                raise ValidationError("Journey is required for journey-based campaigns")
        else:  # bulk campaign
            if not self.channel:
                raise ValidationError("Channel is required for bulk campaigns")
            if not self.template and not self.content:
                raise ValidationError("Either template or content is required for bulk campaigns")

        if self.media_campaign_id:
            if not self.crm_campaign_id:
                raise ValidationError({
                    'media_campaign': 'CRM campaign is required when media_campaign is set.',
                })
            if self.media_campaign.crm_campaign_id != self.crm_campaign_id:
                raise ValidationError({
                    'media_campaign': 'Media campaign must belong to the selected CRM campaign.',
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def update_status(self, new_status, user):
        """
        Update the campaign status with proper tracking

        Args:
            new_status (str): New status from CAMPAIGN_STATUS_CHOICES
            user: User making the status change
        """
        if new_status not in dict(self.CAMPAIGN_STATUS_CHOICES):
            raise ValueError(f"Invalid status: {new_status}")

        # If completing or cancelling an ongoing campaign, update is_ongoing
        if new_status in ['completed', 'cancelled']:
            self.is_ongoing = False
            if not self.end_date:
                self.end_date = timezone.now()

        self.status = new_status
        self.status_changed_at = timezone.now()
        self.status_changed_by = user
        self.save()

    def is_active_or_scheduled(self):
        """Check if campaign is currently active or scheduled to start"""
        if not self.active:
            return False

        if self.status not in ['active', 'scheduled']:
            return False

        now = timezone.now()
        
        # Check start date
        if self.start_date and self.start_date > now:
            return False

        # Check end date only if not ongoing
        if not self.is_ongoing and self.end_date and self.end_date < now:
            return False

        return True

    def can_send_message(self, participant):
        """Check if a message can be sent to a participant"""
        if not self.is_active_or_scheduled():
            return False

        # Check participant status
        if participant.status not in ['active']:
            return False

        # For ongoing campaigns, we only need to check the start date
        if self.is_ongoing:
            return not self.start_date or self.start_date <= timezone.now()

        # For campaigns with end dates, check both start and end
        now = timezone.now()
        if self.start_date and self.start_date > now:
            return False
        if self.end_date and self.end_date < now:
            return False

        return True

    def get_next_send_time(self, last_send_time=None):
        """Calculate the next send time based on campaign type and settings"""
        # First check if campaign is active/scheduled
        if not self.is_active_or_scheduled():
            return None

        if self.campaign_type == 'drip':
            return self._get_drip_next_send_time(last_send_time)
        elif self.campaign_type == 'reminder':
            return self._get_reminder_next_send_time(last_send_time)
        elif self.campaign_type == 'blast':
            return self._get_blast_next_send_time()
        return None

    def _get_drip_next_send_time(self, last_send_time=None):
        """Calculate next send time for drip campaigns"""
        if not hasattr(self, 'drip_schedule'):
            return None

        now = timezone.now()
        tz = pytz.timezone(self.crm_campaign.timezone) if self.crm_campaign else pytz.UTC
        now = now.astimezone(tz)

        # Get the current step for the participant
        participant = self.participants.first()  # This should be called with a specific participant
        if not participant:
            return None

        progress = participant.drip_campaign_progress.first()
        if not progress:
            # Start with the first step
            first_step = self.drip_schedule.message_steps.order_by('order').first()
            if not first_step:
                return None
            progress = DripCampaignProgress.objects.create(
                participant=participant,
                current_step=first_step,
                next_scheduled_interval=now
            )
            return now

        # Get the next step
        current_step = progress.current_step
        if not current_step:
            return None

        next_step = self.drip_schedule.message_steps.filter(order__gt=current_step.order).order_by('order').first()
        if not next_step:
            return None

        # Calculate next send time based on the current step's delay
        if not last_send_time:
            next_time = now
        else:
            next_time = last_send_time + current_step.get_delay_timedelta()

        # Apply business hours and weekend restrictions
        if self.drip_schedule.business_hours_only:
            start_time = self.drip_schedule.start_time
            end_time = self.drip_schedule.end_time

            if next_time.time() < start_time:
                next_time = datetime.combine(next_time.date(), start_time)
            elif next_time.time() > end_time:
                next_time = datetime.combine(next_time.date() + timedelta(days=1), start_time)

        if self.drip_schedule.exclude_weekends:
            while next_time.weekday() >= 5:
                next_time += timedelta(days=1)

        # Update progress
        progress.current_step = next_step
        progress.next_scheduled_interval = next_time
        progress.save()

        return next_time

    def _get_reminder_next_send_time(self, last_send_time=None):
        """Calculate next send time for reminder campaigns"""
        if not hasattr(self, 'reminder_schedule'):
            return None

        # Implementation for reminder campaigns
        pass

    def _get_blast_next_send_time(self):
        """Get send time for blast campaigns"""
        if not hasattr(self, 'blast_schedule'):
            return None
        return self.blast_schedule.send_time

    def move_to_next_step(self, next_step, event_type='enter_step', metadata=None):
        """Move participant to next step and create event"""
        if not self.journey:
            raise ValidationError("Cannot move to next step for bulk campaigns")

        self.current_journey_step = next_step
        self.last_event_at = timezone.now()
        self.save()

        JourneyEvent.objects.create(
            participant=self,
            journey_step=next_step,
            event_type=event_type,
            metadata=metadata,
            created_by=self.last_updated_by
        )

    def replace_variables(self, context):
        """
        Replaces variables in the campaign content with values from the context.
        
        Args:
            context (dict): Dictionary containing values for variables.
                           Should be structured as: {'lead': {...}, 'campaign': {...}, etc.}
        
        Returns:
            str: Content with variables replaced with their values
        """
        # Get the appropriate channel config based on campaign channel
        channel_config = None
        if self.channel == 'email' and self.email_config:
            channel_config = self.email_config
        elif self.channel == 'sms' and self.sms_config:
            channel_config = self.sms_config
        elif self.channel == 'voice' and self.voice_config:
            channel_config = self.voice_config
        elif self.channel == 'chat' and self.chat_config:
            channel_config = self.chat_config
            
        if not channel_config:
            logger.error(f"No channel config found for campaign {self.id}")
            return ""
            
        # Use template if available, otherwise use content
        if channel_config.template:
            return channel_config.template.replace_variables(context)
        elif channel_config.content:
            return replace_variables(channel_config.content, context)
            
        return ""

    def get_retry_strategy(self):
        """Get the effective retry strategy for this campaign"""
        if self.retry_strategy:
            return self.retry_strategy
        # Use default strategy if none specified
        from .nurturing_campaign_base import RetryStrategy
        return RetryStrategy.get_default_strategy()

    def get_max_retries(self):
        """Get the effective max retries for this campaign"""
        if self.max_retries is not None:
            return self.max_retries
        # Use retry strategy default if not overridden
        return self.get_retry_strategy().max_attempts

class BulkCampaignMessageGroup(models.Model):
    """
    Model to group related bulk campaign messages together for a participant.
    This allows handling message sending logic for groups of messages,
    such as regular messages and opt-out messages, at the participant level.
    """
    campaign = models.ForeignKey('LeadNurturingCampaign', on_delete=models.CASCADE, related_name='message_groups')
    participant = models.ForeignKey('LeadNurturingParticipant', on_delete=models.CASCADE, related_name='message_groups')
    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('in_progress', 'In Progress'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled')
        ],
        default='pending'
    )
    metadata = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'bulk_campaign_message_group'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['participant', 'status']),
        ]

    def __str__(self):
        return f"{self.campaign.name} - {self.participant.lead.email} - Group {self.id}"

    def update_status(self, new_status, metadata=None):
        """Update group status and metadata"""
        self.status = new_status
        if metadata:
            if not self.metadata:
                self.metadata = {}
            self.metadata.update(metadata)
        self.save()

class BulkCampaignMessage(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('scheduled', 'Scheduled'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
        ('retry', 'Retry'),
        ('failed_final', 'Failed (max retries exceeded)'),
        ('opened', 'Opened'),
        ('clicked', 'Clicked'),
        ('replied', 'Replied'),
        ('opted_out', 'Opted Out'),
        ('cancelled', 'Cancelled')
    ]

    MESSAGE_TYPES = [
        ('regular', 'Regular Message'),
        ('opt_out_notice', 'Opt-out Notice'),
        ('opt_out_confirmation', 'Opt-out Confirmation')
    ]

    campaign = models.ForeignKey('LeadNurturingCampaign', on_delete=models.CASCADE, related_name='messages')
    participant = models.ForeignKey('LeadNurturingParticipant', on_delete=models.CASCADE, related_name='bulk_messages')
    message_group = models.ForeignKey(
        'BulkCampaignMessageGroup',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages',
        help_text="The message group this message belongs to"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)
    metadata = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Fields for drip campaign message steps
    drip_message_step = models.ForeignKey(
        'DripCampaignMessageStep',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages',
        help_text="The message step that generated this message (for drip campaigns)"
    )
    step_order = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The order of the message step in the drip sequence"
    )

    # New field for reminder campaign messages
    reminder_message = models.ForeignKey(
        'ReminderMessage',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bulk_messages',
        help_text="The reminder message configuration for this message (for reminder campaigns)"
    )

    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default='regular')

    # Retry tracking fields
    retry_count = models.PositiveIntegerField(default=0)
    last_retry_at = models.DateTimeField(null=True, blank=True)

    # Retry configuration (optional - can use campaign-level defaults)
    retry_strategy = models.ForeignKey(
        'RetryStrategy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Custom retry strategy for this message (uses campaign default if not set)"
    )
    max_retries = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override max retries for this message (uses campaign default if not set)"
    )

    # Provider message id (e.g. Twilio SID) when this message was sent; used to match inbound replies
    provider_message_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="Twilio (or other provider) message SID when this bulk message was sent; used to match inbound replies."
    )
    next_eligible_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            'When this message becomes eligible to send again after being deferred '
            '(e.g. by a send cap). Set by the external dispatcher.'
        ),
    )
    deferral_reason = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text='Free-form reason the dispatcher last deferred this message. e.g. "cap:hourly:42".',
    )

    class Meta:
        managed = False
        db_table = 'bulk_campaign_message'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['participant', 'status']),
            models.Index(fields=['scheduled_for']),
            models.Index(fields=['drip_message_step']),
            models.Index(fields=['step_order']),
            models.Index(fields=['reminder_message']),
            models.Index(fields=['message_group']),
            models.Index(fields=['status', 'retry_count']),
            models.Index(fields=['last_retry_at']),
            models.Index(fields=['campaign', 'status', 'scheduled_for']),
        ]
        # Unique constraints to prevent duplicate message scheduling
        constraints = [
            # For blast campaigns: unique per participant, campaign, and message type
            # (when drip_message_step and reminder_message are NULL)
            models.UniqueConstraint(
                fields=['participant', 'campaign', 'message_type'],
                condition=models.Q(
                    drip_message_step__isnull=True,
                    reminder_message__isnull=True
                ),
                name='unique_blast_message_per_participant'
            ),
            # For drip campaigns: unique per participant, drip message step, and message type
            # (when drip_message_step is not NULL)
            models.UniqueConstraint(
                fields=['participant', 'drip_message_step', 'message_type'],
                condition=models.Q(
                    drip_message_step__isnull=False
                ),
                name='unique_drip_message_per_step'
            ),
            # For reminder campaigns: unique per participant, reminder message, and message type
            # (when reminder_message is not NULL)
            models.UniqueConstraint(
                fields=['participant', 'reminder_message', 'message_type'],
                condition=models.Q(
                    reminder_message__isnull=False
                ),
                name='unique_reminder_message_per_reminder'
            ),
        ]

    def __str__(self):
        return f"{self.campaign.name} - {self.participant.lead.email} - {self.status}"

    def clean(self):
        """Validate that the appropriate fields are set based on campaign type"""
        super().clean()
        
        campaign = self.campaign
        if not campaign:
            return
            
        if campaign.campaign_type == 'drip' and not self.drip_message_step:
            raise ValidationError("Drip message step is required for drip campaigns")
        elif campaign.campaign_type == 'reminder' and not self.reminder_message:
            raise ValidationError("Reminder message is required for reminder campaigns")
        elif campaign.campaign_type == 'blast' and (self.drip_message_step or self.reminder_message):
            raise ValidationError("Blast campaigns should not have drip_message_step or reminder_message set")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    # Statuses that mean nothing is wrong right now, so a stale error_message from an
    # earlier attempt must not survive into them.
    NON_ERROR_STATUSES = frozenset(
        {'pending', 'scheduled', 'sent', 'delivered', 'opened', 'clicked', 'replied'}
    )

    def update_status(self, new_status, metadata=None, error_message=None):
        """Update message status, timestamps, and the human-readable reason.

        error_message is what anyone looking at a stuck message reads first, so it has to
        carry the actual reason. It used to be left at a generic "Message failed to send"
        while the real cause sat in metadata, which is how four months of messages dropped
        by a send cap looked like a provider outage.
        """
        self.status = new_status
        now = timezone.now()

        if new_status == 'sent':
            self.sent_at = now
            # If this is an opt-out confirmation message, mark it as sent on the participant
            if self.message_type == 'opt_out_confirmation':
                self.participant.opt_out_message_sent = True
                self.participant.save()
        elif new_status == 'delivered':
            self.delivered_at = now
        elif new_status == 'opened':
            self.opened_at = now
        elif new_status == 'clicked':
            self.clicked_at = now
        elif new_status == 'replied':
            self.replied_at = now

        if metadata:
            if not self.metadata:
                self.metadata = {}
            self.metadata.update(metadata)

        if error_message is not None:
            self.error_message = error_message or None
        elif new_status in self.NON_ERROR_STATUSES:
            self.error_message = None

        self.save()

    def get_retry_strategy(self):
        """Get the effective retry strategy for this message"""
        if self.retry_strategy:
            return self.retry_strategy
        return self.campaign.get_retry_strategy()

    def get_max_retries(self):
        """Get the effective max retries for this message"""
        if self.max_retries is not None:
            return self.max_retries
        return self.campaign.get_max_retries()

    def can_retry(self):
        """Check if message can be retried"""
        return self.status == 'failed' and self.retry_count < self.get_max_retries()

    def mark_for_retry(self, retry_count=None):
        """Mark message for retry by external processor"""
        if not self.can_retry():
            return False
        
        self.retry_count = retry_count or (self.retry_count + 1)
        self.status = 'retry'
        self.last_retry_at = timezone.now()
        self.save()
        return True

    def get_retry_delay_minutes(self):
        """Calculate the delay in minutes for the next retry attempt"""
        if self.retry_count == 0:
            return 0
        
        strategy = self.get_retry_strategy()
        return strategy.get_delay_for_attempt(self.retry_count)

    def can_be_sent(self):
        """Check if the message can be sent"""
        if self.status not in ['pending', 'scheduled', 'retry']:
            return False

        if self.scheduled_for and self.scheduled_for > timezone.now():
            return False

        return self.campaign.can_send_message(self.participant)

    def get_effective_email_config(self):
        """EmailConfig for email-channel bulk sends (drip step, reminder message, or campaign)."""
        campaign = self.campaign
        if campaign.channel != 'email':
            return None
        if self.message_type != 'regular':
            return None
        if campaign.campaign_type == 'drip' and self.drip_message_step:
            return self.drip_message_step.email_config
        if campaign.campaign_type == 'reminder' and self.reminder_message:
            return self.reminder_message.email_config
        return campaign.email_config

    def get_message_content(self, extra_context=None):
        """Resolve message body for bulk sends.

        Email ``outbound_acs`` / legacy ``hosted_mailgun`` uses ``hosted_template_version`` and
        ``replace_template_variables`` (same merge rules as ``send_from_email_config``). Inline
        channel configs use ``MessageTemplate.replace_variables`` / ``replace_variables`` (link
        keyword fallbacks for raw strings).

        extra_context: merged into template context (e.g. ``link`` / ``keyword`` from the processor).
        """
        from shared_services.template_variable_render import (
            build_nested_template_context,
            replace_template_variables,
        )

        from .outbound_email_template import OutboundEmailTemplateVersion

        campaign = self.campaign
        lead = self.participant.lead

        # Opt-out copy: keep replace_variables for {{link.short_link}} fallbacks not driven by TemplateVariable rows.
        opt_ctx = build_nested_template_context(
            lead=lead,
            nurturing_campaign=campaign,
            sender_user=getattr(campaign, 'created_by', None),
            extra=extra_context,
        )
        from shared_services.eav_email_merge import apply_eav_placeholders

        if self.message_type == 'opt_out_notice':
            merged = replace_variables(campaign.initial_opt_out_notice or '', opt_ctx)
            lead_obj = opt_ctx.get('lead')
            if isinstance(lead_obj, Lead):
                return apply_eav_placeholders(text=str(merged), lead=lead_obj)
            return merged
        if self.message_type == 'opt_out_confirmation':
            merged = replace_variables(campaign.opt_out_message or '', opt_ctx)
            lead_obj = opt_ctx.get('lead')
            if isinstance(lead_obj, Lead):
                return apply_eav_placeholders(text=str(merged), lead=lead_obj)
            return merged
        if self.message_type != 'regular':
            return ''

        context = opt_ctx

        def _with_eav_merged(s):
            if s is None:
                return None
            lead_obj = context.get('lead')
            if isinstance(lead_obj, Lead):
                return apply_eav_placeholders(text=str(s), lead=lead_obj)
            return s

        def _try_outbound_acs_email_body(email_config: EmailConfig):
            if email_config.email_content_mode not in (
                EmailConfig.MODE_OUTBOUND_ACS,
                EmailConfig.MODE_HOSTED_MAILGUN,
            ):
                return False, None
            ver = email_config.hosted_template_version
            if not ver or ver.status != OutboundEmailTemplateVersion.STATUS_APPROVED:
                logger.error(
                    'Outbound ACS email misconfigured: hosted_template_version missing or not '
                    'approved (email_config_id=%s nurturing_campaign_id=%s)',
                    getattr(email_config, 'pk', None),
                    campaign.id,
                )
                return True, None
            return True, _with_eav_merged(replace_template_variables(ver.html_body or '', context))

        def _inline_channel_body(channel_config):
            if getattr(channel_config, 'template_id', None):
                return _with_eav_merged(channel_config.template.replace_variables(context))
            raw = (getattr(channel_config, 'content', None) or '').strip()
            if raw:
                return _with_eav_merged(replace_variables(raw, context))
            return None

        if campaign.campaign_type == 'drip' and self.drip_message_step:
            channel_config = self.drip_message_step.get_channel_config()
            if not channel_config:
                logger.error(
                    'No channel config found for drip message step %s',
                    self.drip_message_step.id,
                )
                return ''
            if campaign.channel == 'email' and isinstance(channel_config, EmailConfig):
                is_out, body = _try_outbound_acs_email_body(channel_config)
                if is_out:
                    return body if body is not None else ''
            merged = _inline_channel_body(channel_config)
            if merged is not None:
                return merged
            logger.error(
                'No content found in drip message step %s',
                self.drip_message_step.id,
            )
            return ''

        if campaign.campaign_type == 'reminder' and self.reminder_message:
            rm = self.reminder_message
            if campaign.channel == 'email' and rm.email_config:
                is_out, body = _try_outbound_acs_email_body(rm.email_config)
                if is_out:
                    return body if body is not None else ''
            channel_config = rm.get_channel_config()
            if channel_config:
                merged = _inline_channel_body(channel_config)
                if merged is not None:
                    return merged
            logger.error('No content found in reminder message %s', rm.id)
            return ''

        # Blast, journey, and other types: campaign-level channel config (+ legacy campaign.content).
        if campaign.channel == 'email' and campaign.email_config:
            is_out, body = _try_outbound_acs_email_body(campaign.email_config)
            if is_out:
                return body if body is not None else ''

        channel_config = None
        if campaign.channel == 'email' and campaign.email_config:
            channel_config = campaign.email_config
        elif campaign.channel == 'sms' and campaign.sms_config:
            channel_config = campaign.sms_config
        elif campaign.channel == 'voice' and campaign.voice_config:
            channel_config = campaign.voice_config
        elif campaign.channel == 'chat' and campaign.chat_config:
            channel_config = campaign.chat_config

        if channel_config:
            merged = _inline_channel_body(channel_config)
            if merged is not None:
                return merged

        raw_campaign = (getattr(campaign, 'content', None) or '').strip()
        if raw_campaign:
            return _with_eav_merged(replace_variables(raw_campaign, context))

        logger.error('No content found in campaign %s', campaign.id)
        return ''

    @classmethod
    def check_existing_message(cls, participant, campaign, message_type, drip_message_step=None, reminder_message=None):
        """
        Check if a message already exists for the given parameters.
        Returns the existing message if found, None otherwise.
        
        This method respects the unique constraints for different campaign types:
        - Blast campaigns: unique per participant + campaign + message_type
        - Drip campaigns: unique per participant + drip_message_step + message_type  
        - Reminder campaigns: unique per participant + reminder_message + message_type
        """
        # Base filters
        filters = {
            'participant': participant,
            'campaign': campaign,
            'message_type': message_type,
        }
        
        # Add campaign-specific filters based on campaign type
        if campaign.campaign_type == 'blast':
            # For blast campaigns, ensure drip_message_step and reminder_message are NULL
            filters.update({
                'drip_message_step__isnull': True,
                'reminder_message__isnull': True,
            })
        elif campaign.campaign_type == 'drip':
            # For drip campaigns, require drip_message_step to be set
            if not drip_message_step:
                raise ValueError("drip_message_step is required for drip campaigns")
            filters['drip_message_step'] = drip_message_step
        elif campaign.campaign_type == 'reminder':
            # For reminder campaigns, require reminder_message to be set
            if not reminder_message:
                raise ValueError("reminder_message is required for reminder campaigns")
            filters['reminder_message'] = reminder_message
        
        # Exclude cancelled, failed, and retry messages from the check
        # This prevents creating new messages when there are existing failed/retry messages
        existing_message = cls.objects.filter(
            **filters
        ).exclude(
            status__in=['cancelled', 'failed', 'retry']
        ).first()
        
        return existing_message

    @classmethod
    def check_existing_retry_message(cls, participant, campaign, message_type, drip_message_step=None, reminder_message=None):
        """
        Check if a retry message already exists for the given parameters.
        Returns the existing retry message if found, None otherwise.
        
        This method is specifically for retry logic to find existing retry messages.
        """
        # Base filters
        filters = {
            'participant': participant,
            'campaign': campaign,
            'message_type': message_type,
            'status': 'retry',  # Only look for retry messages
        }
        
        # Add campaign-specific filters based on campaign type
        if campaign.campaign_type == 'blast':
            # For blast campaigns, ensure drip_message_step and reminder_message are NULL
            filters.update({
                'drip_message_step__isnull': True,
                'reminder_message__isnull': True,
            })
        elif campaign.campaign_type == 'drip':
            # For drip campaigns, require drip_message_step to be set
            if not drip_message_step:
                raise ValueError("drip_message_step is required for drip campaigns")
            filters['drip_message_step'] = drip_message_step
        elif campaign.campaign_type == 'reminder':
            # For reminder campaigns, require reminder_message to be set
            if not reminder_message:
                raise ValueError("reminder_message is required for reminder campaigns")
            filters['reminder_message'] = reminder_message
        
        existing_message = cls.objects.filter(**filters).first()
        return existing_message

    @classmethod
    def create_message_safely(cls, participant, campaign, message_type, **kwargs):
        """
        Safely create a bulk campaign message, checking for existing messages first.
        Returns the created message or the existing message if one already exists.
        
        This method prevents duplicate message creation by checking existing messages
        before creating new ones, respecting the unique constraints for each campaign type.
        """
        # Determine campaign-specific parameters
        drip_message_step = kwargs.get('drip_message_step')
        reminder_message = kwargs.get('reminder_message')
        
        # Check for existing message
        existing_message = cls.check_existing_message(
            participant=participant,
            campaign=campaign,
            message_type=message_type,
            drip_message_step=drip_message_step,
            reminder_message=reminder_message
        )
        
        if existing_message:
            return existing_message
        
        # Create new message
        message_data = {
            'participant': participant,
            'campaign': campaign,
            'message_type': message_type,
            **kwargs
        }
        
        # Ensure campaign-specific fields are properly set
        if campaign.campaign_type == 'blast':
            message_data.update({
                'drip_message_step': None,
                'reminder_message': None,
            })
        elif campaign.campaign_type == 'drip':
            if not drip_message_step:
                raise ValueError("drip_message_step is required for drip campaigns")
            message_data['drip_message_step'] = drip_message_step
        elif campaign.campaign_type == 'reminder':
            if not reminder_message:
                raise ValueError("reminder_message is required for reminder campaigns")
            message_data['reminder_message'] = reminder_message
        
        return cls.objects.create(**message_data)

class LeadNurturingParticipant(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='lead_nurturing_participations')
    nurturing_campaign = models.ForeignKey('LeadNurturingCampaign', on_delete=models.CASCADE, related_name='participants')
    current_journey_step = models.ForeignKey('JourneyStep', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_participants')
    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'Active'),
            ('completed', 'Completed'),
            ('exited', 'Exited'),
            ('paused', 'Paused'),
            ('opted_out', 'Opted Out')
        ],
        default='active'
    )
    last_event_at = models.DateTimeField(null=True, blank=True)
    entered_campaign_at = models.DateTimeField(auto_now_add=True)
    exited_campaign_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_lead_nurturing_participants')
    last_updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='updated_lead_nurturing_participants')
    
    # Campaign tracking fields
    last_message_sent_at = models.DateTimeField(null=True, blank=True)
    messages_sent_count = models.PositiveIntegerField(default=0)
    next_scheduled_message = models.DateTimeField(null=True, blank=True)
    opt_out_message_sent = models.BooleanField(
        default=False,
        help_text="Indicates whether the opt-out confirmation message has been sent"
    )
    metadata = models.JSONField(blank=True, null=True)

    # Attribution: campaign-scoped opt-in that triggered enrollment (subscriber, campaign, rule, opt-in message)
    originating_subscription = models.ForeignKey(
        'sms_marketing.SmsSubscriberCampaignSubscription',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='nurturing_participants_enrolled',
        help_text='Campaign-level opt-in subscription that triggered enrollment (subscriber, campaign, opt_in_rule, opt_in_message).',
    )
    media_campaign = models.ForeignKey(
        'planning.MediaCampaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name='nurturing_participants',
        help_text='Snapshotted at enrollment from override / originating_subscription / nurturing_campaign default.',
    )

    class Meta:
        managed = False
        db_table = 'lead_nurturing_participant'
        unique_together = [
            ['lead', 'nurturing_campaign', 'status']
        ]
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['last_event_at']),
            models.Index(fields=['last_message_sent_at']),
            models.Index(fields=['next_scheduled_message']),
            models.Index(fields=['entered_campaign_at']),
            models.Index(fields=['exited_campaign_at']),
            models.Index(fields=['originating_subscription']),
            models.Index(fields=['lead', 'media_campaign']),
        ]

    def __str__(self):
        return f"{self.lead} in {self.nurturing_campaign}"

    def clean(self):
        """Validate participant configuration"""
        super().clean()

        # For journey-based campaigns, journey is required
        if self.nurturing_campaign and self.nurturing_campaign.campaign_type == 'journey' and not self.nurturing_campaign.journey:
            raise ValidationError("Journey is required for journey-based campaigns")

        if self.media_campaign_id and self.nurturing_campaign_id:
            nc_crm_id = self.nurturing_campaign.crm_campaign_id
            if nc_crm_id and self.media_campaign.crm_campaign_id != nc_crm_id:
                raise ValidationError({
                    'media_campaign': 'Media campaign must belong to the nurturing campaign CRM campaign.',
                })

    def move_to_next_step(self, next_step, event_type='enter_step', metadata=None):
        """Move participant to next step and create event"""
        if not self.nurturing_campaign.journey:
            raise ValidationError("Cannot move to next step for bulk campaigns")
            
        self.current_journey_step = next_step
        self.last_event_at = timezone.now()
        self.save()
        
        JourneyEvent.objects.create(
            participant=self,
            journey_step=next_step,
            event_type=event_type,
            metadata=metadata,
            created_by=self.last_updated_by
        )

    def update_campaign_progress(self, message_sent=False, scheduled_time=None):
        """Update campaign progress for bulk campaigns"""
        if not self.nurturing_campaign or self.nurturing_campaign.campaign_type == 'journey':
            return

        now = timezone.now()
        campaign = self.nurturing_campaign

        if message_sent:
            self.messages_sent_count += 1
            self.last_message_sent_at = now

        if scheduled_time:
            self.next_scheduled_message = scheduled_time

        self.save()

        # Update campaign-specific progress
        if campaign.campaign_type == 'drip':
            self._update_drip_progress(now, scheduled_time)
        elif campaign.campaign_type == 'reminder':
            self._update_reminder_progress(now, scheduled_time)
        elif campaign.campaign_type == 'blast':
            self._update_blast_progress(now)

    def _update_drip_progress(self, now, scheduled_time):
        """Update progress for drip campaigns"""
        progress = self.drip_campaign_progress.first()
        if not progress:
            progress = DripCampaignProgress.objects.create(
                participant=self
            )
        
        if self.messages_sent_count > 0:
            progress.last_interval = now
        
        if scheduled_time:
            progress.next_scheduled_interval = scheduled_time
            
        progress.save()

    def _update_reminder_progress(self, now, scheduled_time):
        """Update progress for reminder campaigns"""
        days_before = self._get_days_before(now)
        if days_before is not None and days_before > 0:
            ReminderCampaignProgress.objects.create(
                participant=self,
                days_before=days_before,
                sent_at=now,
                next_scheduled_reminder=scheduled_time
            )

    def _update_blast_progress(self, now):
        """Update progress for blast campaigns"""
        progress, created = BlastCampaignProgress.objects.get_or_create(
            participant=self
        )
        progress.message_sent = True
        progress.sent_at = now
        progress.save()

    def _get_days_before(self, current_time):
        """Helper method to calculate days before for reminder campaigns"""
        if not self.nurturing_campaign or self.nurturing_campaign.campaign_type != 'reminder':
            return 0

        campaign = self.nurturing_campaign
        if not hasattr(campaign, 'reminder_schedule') or not campaign.reminder_schedule:
            return 0

        # Find the next reminder time that hasn't been sent yet
        sent_days = set(
            self.reminder_campaign_progress.values_list('days_before', flat=True)
        )
        
        for reminder in campaign.reminder_schedule.reminder_times.all():
            # Skip reminders with None days_before values
            if reminder.days_before is None:
                continue
            if reminder.days_before not in sent_days:
                return reminder.days_before

        return 0

    def get_campaign_progress(self):
        """Get the current campaign progress"""
        campaign = self.nurturing_campaign
        if not campaign:
            return None

        if campaign.campaign_type == 'drip':
            progress = self.drip_campaign_progress.first()
            if progress:
                return {
                    'last_interval': progress.last_interval,
                    'intervals_completed': progress.intervals_completed,
                    'total_intervals': progress.total_intervals,
                    'next_scheduled_interval': progress.next_scheduled_interval
                }
        elif campaign.campaign_type == 'reminder':
            reminders = self.reminder_campaign_progress.all().order_by('-sent_at')
            next_reminder = reminders.filter(next_scheduled_reminder__isnull=False).first()
            return {
                'reminders_sent': [
                    {
                        'days_before': r.days_before,
                        'sent_at': r.sent_at
                    } for r in reminders
                ],
                'next_reminder': {
                    'days_before': next_reminder.days_before if next_reminder else None,
                    'scheduled_for': next_reminder.next_scheduled_reminder if next_reminder else None
                } if next_reminder else None
            }
        elif campaign.campaign_type == 'blast':
            progress = self.blast_campaign_progress.first()
            if progress:
                return {
                    'message_sent': progress.message_sent,
                    'sent_at': progress.sent_at
                }

        return None

    def can_opt_out(self):
        """
        Check if a participant can opt out of the campaign.
        Returns a tuple of (can_opt_out: bool, reason: str)
        """
        # Check if campaign allows opt-outs
        if not self.nurturing_campaign.enable_opt_out:
            return False, "Opt-out is not enabled for this campaign"

        # Check if participant is already opted out
        if self.status == 'opted_out':
            return False, "Participant has already opted out"

        # Check if campaign is active
        if not self.nurturing_campaign.is_active_or_scheduled():
            return False, "Campaign is not active"

        # Check if participant is in a valid state to opt out
        if self.status not in ['active', 'paused']:
            return False, f"Cannot opt out while in '{self.status}' status"

        return True, "Participant can opt out"

    def opt_out(self, user=None):
        """
        Handle participant opt-out:
        1. Update participant status to opted_out
        2. Set exited_campaign_at timestamp
        3. Cancel all pending/scheduled messages
        4. Update last_updated_by if user provided
        """
        # Check if participant can opt out
        can_opt_out, reason = self.can_opt_out()
        if not can_opt_out:
            raise ValidationError(reason)

        now = timezone.now()
        
        # Update participant status
        self.status = 'opted_out'
        self.exited_campaign_at = now
        if user:
            self.last_updated_by = user
        self.save()

        # Cancel all pending/scheduled messages
        self.bulk_messages.filter(
            status__in=['pending', 'scheduled']
        ).update(
            status='cancelled',
            error_message='Message cancelled due to participant opt-out',
            updated_at=now
        )

        # Create an opt-out event if this is a journey campaign
        if self.nurturing_campaign.campaign_type == 'journey':
            JourneyEvent.objects.create(
                participant=self,
                journey_step=self.current_journey_step,
                event_type='opt_out',
                created_by=user or self.last_updated_by
            )

        # Only create opt-out confirmation message if one hasn't been sent
        if not self.opt_out_message_sent and self.nurturing_campaign.enable_opt_out:
            BulkCampaignMessage.objects.create(
                campaign=self.nurturing_campaign,
                participant=self,
                status='scheduled',
                scheduled_for=now,
                message_type='opt_out_confirmation'
            )

        return True

    def reset_opt_out_message(self, user=None):
        """
        Reset the opt-out message flag to allow resending the opt-out message.
        This is useful when:
        1. The previous opt-out message failed to send
        2. We need to resend the opt-out message
        3. There was an issue with the previous opt-out process
        
        Args:
            user: User performing the reset (optional)
        """
        self.opt_out_message_sent = False
        if user:
            self.last_updated_by = user
        self.save()

        # Log the reset in metadata
        if not self.metadata:
            self.metadata = {}
        self.metadata.update({
            'opt_out_message_reset': {
                'timestamp': timezone.now().isoformat(),
                'user_id': user.id if user else None,
                'reason': 'Manual reset'
            }
        })
        self.save()

        return True
