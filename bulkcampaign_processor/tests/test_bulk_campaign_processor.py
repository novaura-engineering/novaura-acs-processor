"""BulkCampaignProcessor integration tests (send caps)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from bulkcampaign_processor.services.bulk_campaign_processor import (
    BulkCampaignProcessor,
    SendOutcome,
)
from bulkcampaign_processor.services.send_cap_service import ClaimResult


class _CampaignStub:
    """Minimal stand-in for LeadNurturingCampaign in _send_message tests."""

    id = 4242
    channel = 'email'
    campaign_type = 'blast'
    active = True
    status = 'active'
    crm_campaign = None
    blast_schedule = None
    drip_schedule = None
    reminder_schedule = None
    subject = 'Hi'
    name = 'Test'

    def __init__(self):
        self.created_by = MagicMock()
        self.email_config = MagicMock(
            from_endpoint_id=1,
            email_content_mode='inline',
            content='hello',
            template=None,
        )

    def can_send_message(self, participant):
        return True


@pytest.mark.django_db
@patch('bulkcampaign_processor.services.bulk_campaign_processor.resolve_media_campaign_for_participant', return_value=None)
@patch('bulkcampaign_processor.services.bulk_campaign_processor.build_nested_template_context', return_value={})
@patch('bulkcampaign_processor.services.bulk_campaign_processor.ensure_link_published', return_value=True)
@patch('bulkcampaign_processor.services.bulk_campaign_processor.try_claim_send_slot')
def test_send_message_defers_when_cap_blocks(mock_claim, _elp, _btc, _rmfp):
    mock_claim.return_value = ClaimResult(
        allowed=False,
        blocking_cap_id=3,
        blocking_cap_period='hourly',
        next_reset_at=timezone.now() + timedelta(hours=1),
        bucket_ids=(),
        rolling_cap_ids=(),
        claim_token=None,
        claimed_at=None,
    )

    campaign = _CampaignStub()

    participant = MagicMock()
    participant.lead = MagicMock(email='a@b.com')

    update_calls: list[tuple] = []

    def update_status(new_status, metadata=None):
        update_calls.append((new_status, metadata))

    message = SimpleNamespace(
        id=100,
        campaign=campaign,
        participant=participant,
        status='pending',
        message_type='regular',
        retry_count=0,
        message_group=None,
        drip_message_step=None,
        reminder_message=None,
        metadata={},
        provider_message_id=None,
        scheduled_for=timezone.now() - timedelta(seconds=5),
        deferral_reason='',
        can_be_sent=lambda: True,
        get_message_content=lambda extra_context=None: 'body',
        update_status=update_status,
        refresh_from_db=lambda: None,
    )

    proc = BulkCampaignProcessor()
    mock_delivery = MagicMock()
    proc.message_delivery = mock_delivery
    out = proc._send_message(message)

    assert out is SendOutcome.DEFERRED
    mock_delivery.send_message.assert_not_called()
    assert update_calls and update_calls[0][0] == 'scheduled'
    assert message.deferral_reason == 'cap:hourly:3'


class _RetryMessageStub:
    """Minimal BulkCampaignMessage stand-in for process_retry_messages tests."""

    def __init__(self, message_id=500):
        self.id = message_id
        self.campaign = MagicMock()
        self.campaign.is_active_or_scheduled.return_value = True
        self.status = 'retry'
        self.retry_count = 3
        self.next_eligible_at = timezone.now() + timedelta(hours=1)
        self.status_updates: list[tuple] = []

    def update_status(self, new_status, metadata=None):
        self.status_updates.append((new_status, metadata))
        self.status = new_status


def _run_retry_sweep(proc, message):
    """Drive process_retry_messages over exactly one message."""
    with patch(
        'bulkcampaign_processor.services.bulk_campaign_processor.BulkCampaignMessage'
    ) as mock_model:
        chain = mock_model.objects.filter.return_value.select_related.return_value
        chain.order_by.return_value = [message]
        return proc.process_retry_messages()


@pytest.mark.django_db
def test_deferred_retry_does_not_consume_a_retry_or_fail_the_message():
    """A send-cap deferral is not a delivery failure.

    Three deferrals used to exhaust the retry budget and mark the message failed_final,
    which is how ~32k drip messages were dropped while only waiting for capacity.
    """
    proc = BulkCampaignProcessor()
    message = _RetryMessageStub()

    with patch.object(proc, '_send_message', return_value=SendOutcome.DEFERRED), \
         patch.object(proc, '_handle_failed_message_retry') as mock_retry:
        processed = _run_retry_sweep(proc, message)

    mock_retry.assert_not_called()
    assert message.status_updates == []
    assert message.status == 'retry'
    assert message.retry_count == 3
    assert processed == 0


@pytest.mark.django_db
def test_failed_retry_at_max_retries_is_marked_failed_final():
    """A genuine delivery failure still exhausts retries and terminates."""
    proc = BulkCampaignProcessor()
    message = _RetryMessageStub()

    with patch.object(proc, '_send_message', return_value=SendOutcome.FAILED), \
         patch.object(proc, '_handle_failed_message_retry', return_value=False):
        _run_retry_sweep(proc, message)

    assert message.status_updates == [('failed_final', {'error': 'Max retries exceeded'})]


@pytest.mark.django_db
def test_failed_retry_below_max_is_scheduled_again():
    proc = BulkCampaignProcessor()
    message = _RetryMessageStub()

    with patch.object(proc, '_send_message', return_value=SendOutcome.FAILED), \
         patch.object(proc, '_handle_failed_message_retry', return_value=True) as mock_retry:
        _run_retry_sweep(proc, message)

    mock_retry.assert_called_once_with(message)
    assert message.status_updates == []


@pytest.mark.django_db
def test_sent_retry_counts_as_processed():
    proc = BulkCampaignProcessor()
    message = _RetryMessageStub()

    with patch.object(proc, '_send_message', return_value=SendOutcome.SENT), \
         patch.object(proc, '_handle_failed_message_retry') as mock_retry:
        processed = _run_retry_sweep(proc, message)

    mock_retry.assert_not_called()
    assert processed == 1
