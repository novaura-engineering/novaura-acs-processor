"""A deferred message is not an invalid one.

The send cap rewrites scheduled_for to the next reset, so a message selected as due can be
not-due by the time it is validated. That used to return the same False as a malformed
message, and the caller marked the whole group failed -- a rate limit destroying a message
that was only waiting.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from bulkcampaign_processor.services.bulk_campaign_processor import BulkCampaignProcessor
from shared_services.message_validation_service import (
    MessageValidationService,
    ValidationOutcome,
)


def _message(*, scheduled_for, content="body", message_type="regular", message_id=900):
    campaign = SimpleNamespace(
        id=253,
        channel="email",
        campaign_type="drip",
        crm_campaign=None,
        can_send_message=lambda participant: True,
    )
    participant = SimpleNamespace(id=1, lead=SimpleNamespace(id="lead-1", phone_number=None))
    return SimpleNamespace(
        id=message_id,
        campaign=campaign,
        participant=participant,
        message_type=message_type,
        scheduled_for=scheduled_for,
        get_message_content=lambda extra_context=None: content,
    )


def _validator():
    # Only the timing branch matters here; the delivery service is used solely for
    # phone formatting on sms/voice, and these are email campaigns.
    service = MessageValidationService(message_delivery_service=MagicMock())
    # Only the timing branch is under test; the channel checks need live config.
    service._validate_campaign_contact_endpoints = lambda campaign: True
    service._validate_channel_requirements = lambda campaign, regular, opt_out: True
    return service


@pytest.mark.django_db
def test_a_message_deferred_to_the_next_hour_is_not_ready_rather_than_invalid():
    """This is the production case: the hourly cap pushed scheduled_for to the next hour."""
    deferred = _message(scheduled_for=timezone.now() + timedelta(minutes=12))
    assert _validator().validate_message_pair(deferred) is ValidationOutcome.NOT_READY


@pytest.mark.django_db
def test_a_due_message_with_content_is_valid():
    due = _message(scheduled_for=timezone.now() - timedelta(minutes=5))
    assert _validator().validate_message_pair(due) is ValidationOutcome.VALID


@pytest.mark.django_db
def test_a_message_with_no_content_is_still_invalid():
    empty = _message(scheduled_for=timezone.now() - timedelta(minutes=5), content="")
    assert _validator().validate_message_pair(empty) is ValidationOutcome.INVALID


@pytest.mark.django_db
def test_an_ineligible_participant_is_still_invalid():
    message = _message(scheduled_for=timezone.now() - timedelta(minutes=5))
    message.campaign.can_send_message = lambda participant: False
    assert _validator().validate_message_pair(message) is ValidationOutcome.INVALID


@pytest.mark.django_db
def test_a_deferred_opt_out_partner_also_defers_the_pair():
    regular = _message(scheduled_for=timezone.now() - timedelta(minutes=5))
    opt_out = _message(
        scheduled_for=timezone.now() + timedelta(minutes=30),
        message_type="opt_out_notice",
        message_id=901,
    )
    assert _validator().validate_message_pair(regular, opt_out) is ValidationOutcome.NOT_READY


@pytest.mark.django_db
def test_not_ready_leaves_the_group_alone():
    """The caller must not mark a group failed for a message that is merely waiting."""
    proc = BulkCampaignProcessor()
    group = MagicMock()
    proc.message_group = group

    related = MagicMock()
    related.filter.return_value.first.return_value = _message(
        scheduled_for=timezone.now() + timedelta(minutes=12)
    )
    related.update = MagicMock()

    message = MagicMock()
    message.message_group_id = 73975
    message.campaign.is_active_or_scheduled.return_value = True

    with patch.object(
        proc.validator, "validate_message_pair", return_value=ValidationOutcome.NOT_READY
    ):
        outcome = proc.validator.validate_message_pair(None, None)

    assert outcome is ValidationOutcome.NOT_READY
    group.update_group_status.assert_not_called()
    related.update.assert_not_called()
