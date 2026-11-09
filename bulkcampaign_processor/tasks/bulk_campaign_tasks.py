import logging
from django.db.models import Q

from external_models.models.nurturing_campaigns import LeadNurturingCampaign
from bulkcampaign_processor.services.bulk_campaign_processor import BulkCampaignProcessor

logger = logging.getLogger(__name__)


def process_bulk_campaigns(should_stop=None):
    """
    Process all active bulk campaigns.

    Run every few minutes: `manage.py process_bulk_campaigns --loop` in production.

    Args:
        should_stop: optional callable; checked between campaigns so a stopping
            service finishes the campaign it is on and exits.
    """
    processor = BulkCampaignProcessor()
    processed_count = 0

    # Find all active bulk campaigns
    campaigns = LeadNurturingCampaign.objects.filter(
        Q(status='active') | Q(status='scheduled'),
        campaign_type__in=['drip', 'reminder', 'blast']
    ).select_related(
        'drip_schedule',
        'reminder_schedule',
        'blast_schedule'
    )

    for campaign in campaigns:
        if should_stop is not None and should_stop():
            logger.info('process_bulk_campaigns_stopping_early')
            break
        try:
            count = processor.process_campaign(campaign)
            processed_count += count
        except Exception as e:
            logger.exception(f"Error processing campaign {campaign.id}: {str(e)}")
            logger.error(f"Campaign details - Type: {campaign.campaign_type}, Status: {campaign.status}, Name: {campaign.name}")

    return processed_count


def process_due_messages(should_stop=None, processor=None):
    """
    Process all messages that are due to be sent.

    Run continuously: `manage.py process_due_messages --loop` in production. Pass the
    same processor across passes to keep its per-process state (stale-claim throttle).
    """
    processor = processor or BulkCampaignProcessor()
    return processor.process_due_messages(should_stop=should_stop)


def process_retry_messages():
    """
    Process all messages that are marked for retry and are due to be sent
    This should be run frequently (e.g., every 5 minutes) by a scheduled task
    """
    processor = BulkCampaignProcessor()
    processed_count = processor.process_retry_messages()
    return processed_count
