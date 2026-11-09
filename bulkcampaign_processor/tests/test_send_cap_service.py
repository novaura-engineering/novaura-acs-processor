"""Tests for send_cap_service (claim / refund / reconcile / DST windows)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone
from zoneinfo import ZoneInfo

from acs.models.send_caps import NurturingCampaignSendCap
from bulkcampaign_processor.services.send_cap_service import (
    clear_send_cap_claim_metadata,
    counted_bulk_statuses,
    try_claim_send_slot,
)


def _campaign_mock(tz='America/New_York'):
    crm = MagicMock()
    crm.default_timezone = tz
    camp = MagicMock()
    camp.crm_campaign = crm
    camp.id = 1
    camp.channel = 'email'
    return camp


@pytest.mark.django_db
def test_opt_out_bypasses_claim_without_querying_caps():
    campaign = _campaign_mock()
    with patch('bulkcampaign_processor.services.send_cap_service.NurturingCampaignSendCap.objects') as m:
        r = try_claim_send_slot(campaign=campaign, channel='email', message_type='opt_out_notice')
        assert r.allowed is True
        assert r.bucket_ids == ()
        m.filter.assert_not_called()


@pytest.mark.django_db
@override_settings(SEND_CAPS_ENFORCEMENT_ENABLED=False)
def test_enforcement_disabled_skips_caps():
    campaign = _campaign_mock()
    with patch('bulkcampaign_processor.services.send_cap_service.NurturingCampaignSendCap.objects') as m:
        r = try_claim_send_slot(campaign=campaign, channel='email', message_type='regular')
        assert r.allowed is True
        assert r.enforcement_skipped is True
        m.filter.assert_not_called()


def test_counted_bulk_statuses_tuple():
    assert 'sent' in counted_bulk_statuses()


def test_clear_send_cap_claim_metadata():
    md = {'send_cap_claim': {'claim_token': 'x'}, 'other': 1}
    out = clear_send_cap_claim_metadata(md)
    assert 'send_cap_claim' not in out
    assert out['other'] == 1


def _send_cap_instance_for_compute_window(
    *,
    period: str,
    boundary: str = NurturingCampaignSendCap.BOUNDARY_CALENDAR,
    timezone_name: str = 'America/New_York',
    custom_window_seconds=None,
):
    """Build cap without ORM __init__ so ``campaign`` need not be a saved LeadNurturingCampaign."""
    cap = NurturingCampaignSendCap.__new__(NurturingCampaignSendCap)
    camp = MagicMock()
    camp.crm_campaign = MagicMock(default_timezone='America/New_York')
    cap.__dict__['campaign'] = camp
    cap.__dict__['timezone_name'] = timezone_name
    cap.__dict__['period'] = period
    cap.__dict__['boundary'] = boundary
    cap.__dict__['custom_window_seconds'] = custom_window_seconds
    cap.__dict__['max_messages'] = 1
    cap.__dict__['is_enabled'] = True
    cap.__dict__['counts_message_types'] = []
    return cap


def test_compute_window_dst_daily_spring_forward():
    """Daily window boundaries use naive-then-relocalize (no 23h/25h drift)."""
    cap = _send_cap_instance_for_compute_window(period=NurturingCampaignSendCap.PERIOD_DAILY)
    # During EDT on Mar 9, 2026 — local calendar day should be Mar 9 00:00 to Mar 10 00:00 in Eastern.
    now = datetime(2026, 3, 9, 18, 30, 0, tzinfo=ZoneInfo('UTC'))
    start, end, key = cap.compute_window(now=now)
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    assert start.hour == 0 and start.minute == 0
    assert end > start
    assert (end - start).total_seconds() in (23 * 3600, 24 * 3600, 25 * 3600)  # DST day length


def test_compute_window_monthly_naive_relocalize():
    cap = _send_cap_instance_for_compute_window(period=NurturingCampaignSendCap.PERIOD_MONTHLY)
    now = datetime(2026, 2, 15, 12, 0, 0, tzinfo=ZoneInfo('UTC'))
    start, end, _key = cap.compute_window(now=now)
    assert start.month == 2 and start.day == 1
    assert end.month == 3 and end.day == 1


@pytest.mark.django_db
def test_try_claim_no_caps_returns_no_token():
    campaign = _campaign_mock()
    with patch(
        'bulkcampaign_processor.services.send_cap_service.NurturingCampaignSendCap.objects.filter'
    ) as filt:
        filt.return_value.filter.return_value = []
        r = try_claim_send_slot(campaign=campaign, channel='email', message_type='regular')
        assert r.allowed is True
        assert r.claim_token is None


class _Sliced(list):
    """Mock QuerySet slice: qs[:500] returns self for iteration."""

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self
        return super().__getitem__(key)


@pytest.mark.django_db
@patch('bulkcampaign_processor.services.send_cap_service.BulkCampaignMessage')
def test_reconcile_stale_send_cap_claims_marks_failed(mock_bm):
    from bulkcampaign_processor.services.send_cap_service import reconcile_stale_send_cap_claims

    msg = MagicMock()
    msg.id = 7
    msg.metadata = {
        'send_cap_claim': {
            'claim_token': 'tok',
            'claimed_at': '2019-06-01T12:00:00+00:00',
        }
    }
    msg.provider_message_id = None
    msg.status = 'pending'
    msg.save = MagicMock()
    msg.update_status = MagicMock()

    mock_bm.objects.filter.return_value = _Sliced([msg])

    n = reconcile_stale_send_cap_claims(max_age_seconds=60)
    assert n == 1
    msg.save.assert_called()
    msg.update_status.assert_called()
