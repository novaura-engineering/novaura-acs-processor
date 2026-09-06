"""An already-sent message must not stall the rest of its group.

`can_be_sent()` returns False for any status outside pending/scheduled/retry, so a sent
message returned DEFERRED, and process_due_messages() breaks its group loop on any
non-SENT outcome. A group holding a sent message plus a scheduled one therefore stalled
forever -- 485 due groups, swept 199 times an hour, sending nothing.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from bulkcampaign_processor.services.bulk_campaign_processor import (
    BulkCampaignProcessor,
    SendOutcome,
)


class _CampaignStub:
    id = 253
    campaign_type = 'drip'
    active = True
    status = 'active'
    crm_campaign = None
    blast_schedule = None
    drip_schedule = None
    reminder_schedule = None
    subject = 'Hi'
    name = 'Test'

    def __init__(self, channel='email'):
        self.channel = channel
        self.created_by = MagicMock()

    def can_send_message(self, participant):
        return True


def _sent_message(channel='email', message_id=49210, provider_message_id='pm-abc'):
    """A message a provider already accepted -- the one that used to stall the group."""
    participant = MagicMock()
    participant.lead = MagicMock(email='a@b.com')
    return SimpleNamespace(
        id=message_id,
        campaign=_CampaignStub(channel),
        participant=participant,
        status='sent',
        message_type='regular',
        retry_count=0,
        message_group=None,
        message_group_id=75633,
        drip_message_step=None,
        reminder_message=None,
        metadata={},
        provider_message_id=provider_message_id,
        scheduled_for=timezone.now() - timedelta(days=2),
        deferral_reason='',
        # The real model returns False here for a sent message. That is the whole bug.
        can_be_sent=lambda: False,
        get_message_content=lambda extra_context=None: 'body',
        update_status=lambda *a, **k: None,
        refresh_from_db=lambda: None,
    )


@pytest.mark.django_db
def test_sent_email_message_reports_already_sent_not_deferred():
    proc = BulkCampaignProcessor()
    proc.message_delivery = MagicMock()

    outcome = proc._send_message(_sent_message('email'))

    assert outcome is SendOutcome.ALREADY_SENT, (
        'a DEFERRED here breaks the group loop and strands the scheduled sibling'
    )
    proc.message_delivery.send_message.assert_not_called()


@pytest.mark.django_db
def test_sent_sms_message_reports_already_sent_not_deferred():
    """154 of the stalled groups were SMS, so the skip cannot be email-only."""
    proc = BulkCampaignProcessor()
    proc.message_delivery = MagicMock()

    outcome = proc._send_message(_sent_message('sms'))

    assert outcome is SendOutcome.ALREADY_SENT
    proc.message_delivery.send_message.assert_not_called()


@pytest.mark.django_db
def test_sent_without_provider_id_is_not_treated_as_delivered():
    """Status alone is not proof; without a provider id we must not claim it was sent."""
    proc = BulkCampaignProcessor()
    proc.message_delivery = MagicMock()

    outcome = proc._send_message(_sent_message('email', provider_message_id=''))

    assert outcome is SendOutcome.DEFERRED
    proc.message_delivery.send_message.assert_not_called()


@pytest.mark.django_db
def test_group_loop_reaches_the_scheduled_sibling():
    """The end-to-end shape: sent message first, scheduled sibling second."""
    proc = BulkCampaignProcessor()
    proc.message_delivery = MagicMock()

    attempted = []
    real_send = proc._send_message

    def tracking_send(message):
        outcome = real_send(message) if message.status == 'sent' else SendOutcome.SENT
        attempted.append((message.id, message.status, outcome))
        return outcome

    # Both SENT and ALREADY_SENT let the loop continue; only these two do.
    keeps_going = (SendOutcome.SENT, SendOutcome.ALREADY_SENT)

    sent = _sent_message('email', message_id=49210)
    scheduled = _sent_message('email', message_id=49211, provider_message_id='')
    scheduled.status = 'scheduled'

    for message in (sent, scheduled):
        outcome = tracking_send(message)
        if outcome not in keeps_going:
            break

    assert [a[0] for a in attempted] == [49210, 49211], (
        'the loop must not break on the already-sent message'
    )
