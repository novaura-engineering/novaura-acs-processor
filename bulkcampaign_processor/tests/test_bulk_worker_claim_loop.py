"""The bulk worker claims groups, skips known-blocked work unloaded, and loops in-process.

Ten worker tasks walked the same due list in the same order. Each checked "already
sent?", queued on the send-cap bucket lock, then sent anyway: one message went to
Postmark ten times in three seconds, ~1,000 sends a day carried ~270 distinct messages,
and the duplicates spent the campaigns' daily caps. Each pass also loaded every due row
with ~20 joined relations -- ~16k of them, all waiting on a send cap -- which was most of
the CRM database's read traffic.

These drive the real process_due_messages() / try_claim_send_slot() / PassLoop.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from django.utils import timezone

from bulkcampaign_processor.services.bulk_campaign_processor import (
    CAP_SKIPPABLE_STATUSES,
    BulkCampaignProcessor,
    SendOutcome,
    ValidationOutcome,
)
from bulkcampaign_processor.services.send_cap_service import (
    ClaimResult,
    reconcile_stale_send_cap_claims,
    try_claim_send_slot,
)
from bulkcampaign_processor.utils.pass_loop import PassLoop

MODULE = 'bulkcampaign_processor.services.bulk_campaign_processor'


def _row(message_id, group_id, campaign_id=254, message_type='regular', status='scheduled'):
    return (message_id, group_id, campaign_id, message_type, status)


def _message(message_id, group_id, campaign_id=254, status='scheduled'):
    return SimpleNamespace(
        id=message_id,
        message_type='regular',
        status=status,
        message_group=SimpleNamespace(id=group_id, status='pending'),
        message_group_id=group_id,
        campaign=SimpleNamespace(id=campaign_id, is_active_or_scheduled=lambda: True),
    )


def _denied(next_reset_at, cap_id=3, period='daily'):
    return ClaimResult(
        allowed=False,
        blocking_cap_id=cap_id,
        blocking_cap_period=period,
        next_reset_at=next_reset_at,
        bucket_ids=(),
        rolling_cap_ids=(),
        claim_token=None,
        claimed_at=None,
    )


class _GroupMessages(list):
    """A group's messages queryset: iterable, plus what the sweep calls on it."""

    def filter(self, **kwargs):
        return _GroupMessages([m for m in self if m.message_type == kwargs.get('message_type')])

    def first(self):
        return self[0] if self else None

    def update(self, **kwargs):
        return len(self)


class _Sweep:
    """Runs process_due_messages() over a fake due listing, recording what it touched."""

    def __init__(self, rows, locked_groups=(), vanished=()):
        self.rows = rows
        self.locked_groups = set(locked_groups)  # held by another worker
        self.vanished = set(vanished)  # sent/rescheduled between listing and lock
        self.loaded = []
        self.attempted = []
        self.bulk_updates = []

    def run(self, proc, send, should_stop=None):
        due_qs = MagicMock()
        due_qs.order_by.return_value.values_list.return_value = list(self.rows)

        def reread(id):
            self.loaded.append(id)
            found = MagicMock()
            row = next(r for r in self.rows if r[0] == id)
            found.select_related.return_value.first.return_value = (
                None if id in self.vanished else _message(id, row[1], row[2], row[4])
            )
            return found

        due_qs.filter.side_effect = reread

        def send_and_record(message):
            self.attempted.append(message.id)
            return send(proc, message)

        def group_messages_or_bulk_update(**kwargs):
            qs = MagicMock()
            if 'id__in' in kwargs:
                def record(**fields):
                    self.bulk_updates.append((kwargs, fields))
                    return len(kwargs['id__in'])
                qs.update.side_effect = record
                return qs
            group = kwargs['message_group']
            row = next(r for r in self.rows if r[1] == group.id)
            qs.select_related.return_value.order_by.return_value = _GroupMessages(
                [_message(row[0], row[1], row[2])]
            )
            return qs

        with patch(f'{MODULE}.reconcile_stale_send_cap_claims', return_value=0), patch(
            f'{MODULE}.BulkCampaignMessage'
        ) as mock_model, patch.object(proc, '_due_messages', return_value=due_qs), patch.object(
            proc, '_lock_group', side_effect=lambda gid: gid not in self.locked_groups
        ), patch.object(proc, '_send_message', side_effect=send_and_record), patch.object(
            proc.validator, 'validate_message_pair', return_value=ValidationOutcome.VALID
        ):
            mock_model.objects.filter.side_effect = group_messages_or_bulk_update
            proc.message_group = MagicMock()
            return proc.process_due_messages(should_stop=should_stop)


def _sends_ok(proc, message):
    return SendOutcome.SENT


@pytest.mark.django_db
def test_a_group_held_by_another_worker_is_neither_loaded_nor_sent():
    proc = BulkCampaignProcessor()
    sweep = _Sweep([_row(1, 10), _row(2, 20)], locked_groups={10})

    delivered = sweep.run(proc, _sends_ok)

    assert sweep.attempted == [2], 'the locked group belongs to the other worker'
    assert sweep.loaded == [2], 'nothing is read for a group this worker does not hold'
    assert delivered == 1


@pytest.mark.django_db
def test_a_message_sent_elsewhere_after_listing_is_not_sent_again():
    """The listing is not a claim: the row is re-read as due under the group lock."""
    proc = BulkCampaignProcessor()
    sweep = _Sweep([_row(1, 10)], vanished={1})

    delivered = sweep.run(proc, _sends_ok)

    assert sweep.attempted == []
    assert delivered == 0


@pytest.mark.django_db
def test_once_a_cap_denies_the_rest_of_that_campaign_is_deferred_in_bulk_unloaded():
    proc = BulkCampaignProcessor()
    reset = timezone.now() + timedelta(hours=20)
    sweep = _Sweep([_row(1, 10), _row(2, 20), _row(3, 30), _row(4, 40, campaign_id=253)])

    def deny_254(proc_, message):
        if message.campaign.id == 254:
            proc_._cap_blocks[(254, 'regular')] = _denied(reset)
            return SendOutcome.DEFERRED
        return SendOutcome.SENT

    delivered = sweep.run(proc, deny_254)

    assert sweep.attempted == [1, 4], 'only the first 254 message pays for a claim attempt'
    assert sweep.loaded == [1, 4], 'messages 2 and 3 are never loaded'
    assert delivered == 1

    [(where, fields)] = sweep.bulk_updates
    assert where['id__in'] == [2, 3]
    # Guarded so a row another worker sent meanwhile is left alone.
    assert set(where['status__in']) == set(CAP_SKIPPABLE_STATUSES)
    assert where['scheduled_for__lte'] <= timezone.now()
    # Same state _send_message leaves on a denied claim.
    assert fields['status'] == 'scheduled'
    assert fields['scheduled_for'] == reset
    assert fields['next_eligible_at'] == reset
    assert fields['deferral_reason'] == 'cap:daily:3'


@pytest.mark.django_db
def test_a_cap_block_never_skips_a_failed_message_which_needs_retry_handling():
    proc = BulkCampaignProcessor()
    reset = timezone.now() + timedelta(hours=1)
    sweep = _Sweep([_row(1, 10), _row(2, 20, status='failed')])

    def deny(proc_, message):
        proc_._cap_blocks[(254, 'regular')] = _denied(reset)
        return SendOutcome.DEFERRED

    with patch.object(proc, '_handle_failed_message_retry', return_value=True) as retry:
        sweep.run(proc, deny)

    assert sweep.loaded == [1, 2]
    assert sweep.bulk_updates == []
    assert retry.called


@pytest.mark.django_db
def test_a_cap_block_from_a_previous_pass_is_forgotten():
    """A refund or a cap edit takes effect on the next pass, not after the reset."""
    proc = BulkCampaignProcessor()
    proc._cap_blocks[(254, 'regular')] = _denied(timezone.now() + timedelta(hours=1))
    sweep = _Sweep([_row(1, 10)])

    sweep.run(proc, _sends_ok)

    assert sweep.attempted == [1]


@pytest.mark.django_db
def test_outside_operating_hours_the_rest_of_the_campaign_is_skipped_untouched():
    proc = BulkCampaignProcessor()
    sweep = _Sweep([_row(1, 10), _row(2, 20), _row(3, 30, campaign_id=253)])

    def closed_254(proc_, message):
        if message.campaign.id == 254:
            proc_._outside_hours_campaigns.add(254)
            return SendOutcome.DEFERRED
        return SendOutcome.SENT

    sweep.run(proc, closed_254)

    assert sweep.attempted == [1, 3]
    assert sweep.bulk_updates == [], 'an hours deferral writes nothing, as before'


@pytest.mark.django_db
def test_a_stop_request_ends_the_sweep_between_groups():
    proc = BulkCampaignProcessor()
    sweep = _Sweep([_row(1, 10), _row(2, 20), _row(3, 30)])
    stop_after = iter([False, True, True])

    sweep.run(proc, _sends_ok, should_stop=lambda: next(stop_after))

    assert sweep.attempted == [1]


@pytest.mark.django_db
def test_a_message_without_a_group_is_never_sent_unlocked():
    proc = BulkCampaignProcessor()
    sweep = _Sweep([_row(1, None), _row(2, 20)])

    sweep.run(proc, _sends_ok)

    assert sweep.attempted == [2]


@pytest.mark.django_db
def test_the_denied_claim_is_remembered_for_the_campaign_and_type():
    """_send_message is what feeds the sweep's memo."""
    proc = BulkCampaignProcessor()
    reset = timezone.now() + timedelta(hours=3)
    campaign = SimpleNamespace(
        id=254, channel='email', campaign_type='drip', crm_campaign=None, created_by=None,
        can_send_message=lambda p: True,
    )
    message = MagicMock()
    message.campaign = campaign
    message.status = 'scheduled'
    message.message_type = 'regular'
    message.provider_message_id = None
    message.metadata = {}
    message.can_be_sent.return_value = True
    message.scheduled_for = timezone.now() - timedelta(minutes=1)

    with patch(f'{MODULE}.try_claim_send_slot', return_value=_denied(reset)), patch.object(
        proc, '_resolve_link_for_bulk_message', return_value=(None, None, None, None, '')
    ), patch.object(proc, '_get_campaign_channel_config', return_value=None), patch(
        f'{MODULE}.build_nested_template_context', return_value={}
    ), patch(f'{MODULE}.resolve_media_campaign_for_participant', return_value=None):
        outcome = proc._send_message(message)

    assert outcome is SendOutcome.DEFERRED
    assert proc._cap_blocks[(254, 'regular')].next_reset_at == reset


@pytest.mark.django_db
def test_stale_claims_are_reconciled_at_most_once_per_interval():
    proc = BulkCampaignProcessor()
    with patch(f'{MODULE}.reconcile_stale_send_cap_claims', return_value=0) as reconcile, patch.object(
        proc, '_due_messages'
    ) as due:
        due.return_value.order_by.return_value.values_list.return_value = []
        proc.process_due_messages()
        proc.process_due_messages()

    assert reconcile.call_count == 1


# --- send-cap service --------------------------------------------------------------


def _calendar_cap(cap_id, period, window_end):
    cap = MagicMock()
    cap.id = cap_id
    cap.period = period
    cap.max_messages = 75
    cap.boundary = 'calendar'
    cap.BOUNDARY_CALENDAR = 'calendar'
    cap.effective_counted_message_types.return_value = ['regular']
    cap.compute_window.return_value = (window_end - timedelta(hours=1), window_end, f'k{cap_id}')
    return cap


@pytest.mark.django_db
def test_a_message_blocked_by_daily_and_hourly_caps_waits_for_the_daily_reset():
    """Deferring to the hourly reset re-swept the whole backlog every hour for nothing."""
    now = timezone.now()
    hourly = _calendar_cap(4, 'hourly', now + timedelta(minutes=40))
    daily = _calendar_cap(3, 'daily', now + timedelta(hours=9))
    full_bucket = SimpleNamespace(pk=1, count=75)

    with patch(
        'bulkcampaign_processor.services.send_cap_service.NurturingCampaignSendCap'
    ) as caps, patch(
        'bulkcampaign_processor.services.send_cap_service.NurturingCampaignSendBucket'
    ) as buckets:
        caps.BOUNDARY_CALENDAR = 'calendar'
        caps.objects.filter.return_value.filter.return_value = [hourly, daily]
        buckets.objects.get_or_create.return_value = (full_bucket, False)
        buckets.objects.select_for_update.return_value.get.return_value = full_bucket
        result = try_claim_send_slot(
            campaign=SimpleNamespace(id=254), channel='email', message_type='regular', now=now
        )

    assert result.allowed is False
    assert result.blocking_cap_id == 3
    assert result.blocking_cap_period == 'daily'
    assert result.next_reset_at == daily.compute_window.return_value[1]


@pytest.mark.django_db
def test_stale_claim_reconcile_reads_only_rows_that_hold_a_claim():
    with patch('bulkcampaign_processor.services.send_cap_service.BulkCampaignMessage') as bm:
        bm.objects.filter.return_value.__getitem__.return_value = []
        reconcile_stale_send_cap_claims()

    kwargs = bm.objects.filter.call_args.kwargs
    assert kwargs['metadata__has_key'] == 'send_cap_claim'


# --- scheduler -----------------------------------------------------------------------


@pytest.mark.django_db
def test_drip_scheduler_leaves_out_participants_with_nothing_to_schedule_in_sql():
    proc = BulkCampaignProcessor()
    from external_models.models.nurturing_campaigns import LeadNurturingCampaign

    sql = str(proc._drip_participants_to_schedule(LeadNurturingCampaign(id=254)).query).upper()

    assert 'NOT (EXISTS' in sql or 'NOT EXISTS' in sql
    assert 'CURRENT_STEP_ID' in sql
    assert '"BULK_CAMPAIGN_MESSAGE"' in sql
    assert '"DRIP_CAMPAIGN_PROGRESS"' in sql


@pytest.mark.django_db
def test_bulk_campaign_pass_stops_between_campaigns():
    from bulkcampaign_processor.tasks import bulk_campaign_tasks

    campaigns = [SimpleNamespace(id=i, campaign_type='drip', status='active', name='') for i in (1, 2, 3)]
    stops = iter([False, True])
    with patch.object(bulk_campaign_tasks, 'LeadNurturingCampaign') as model, patch.object(
        bulk_campaign_tasks, 'BulkCampaignProcessor'
    ) as proc_cls:
        model.objects.filter.return_value.select_related.return_value = campaigns
        proc_cls.return_value.process_campaign.return_value = 1
        bulk_campaign_tasks.process_bulk_campaigns(should_stop=lambda: next(stops))

    assert proc_cls.return_value.process_campaign.call_args_list == [call(campaigns[0])]


# --- loop ----------------------------------------------------------------------------


def test_pass_loop_runs_passes_and_survives_a_failing_one():
    results = iter([RuntimeError('db went away'), 3])

    def run_pass(should_stop):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    loop = PassLoop('t', run_pass, interval_seconds=0)
    with patch('bulkcampaign_processor.utils.pass_loop.close_old_connections') as close:
        assert loop.run(max_passes=2) == 2
    assert close.call_count == 4, 'connections recycled before and after every pass'


def test_pass_loop_stop_ends_the_wait_and_reaches_the_pass():
    seen = []

    def run_pass(should_stop):
        seen.append(should_stop())
        loop.stop()

    loop = PassLoop('t', run_pass, interval_seconds=3600)
    with patch('bulkcampaign_processor.utils.pass_loop.close_old_connections'):
        passes = loop.run()

    assert passes == 1, 'a stop during the hour-long wait ends the loop at once'
    assert seen == [False]
