"""One regular message per group per sweep, driven through the real sweep.

A drip group accumulates one message per step. When two steps fell overdue together the
group loop delivered both seconds apart -- 25 such pairs went out on 4-5 September, one
recipient getting steps 142 and 143 within the same second. Spacing is supposed to come
from scheduled_for, and being late must not cost a message its spacing.

These drive the real process_due_messages() rather than re-implementing its rules, so a
change to the loop breaks them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bulkcampaign_processor.services.bulk_campaign_processor import (
    BulkCampaignProcessor,
    SendOutcome,
    ValidationOutcome,
)


def _msg(message_id, message_type='regular', status='scheduled'):
    return SimpleNamespace(
        id=message_id,
        message_type=message_type,
        status=status,
        message_group=SimpleNamespace(id=999, status='pending'),
        message_group_id=999,
        campaign=SimpleNamespace(
            id=253,
            is_active_or_scheduled=lambda: True,
        ),
    )


class _RelatedQuerySet(list):
    """Stands in for the related_messages queryset: iterable, plus the bits used on it."""

    def __init__(self, items):
        super().__init__(items)
        self.updated_with = []

    def filter(self, **kwargs):
        wanted = kwargs.get('message_type')
        return _RelatedQuerySet([m for m in self if m.message_type == wanted])

    def first(self):
        return self[0] if self else None

    def update(self, **kwargs):
        self.updated_with.append(kwargs)
        return len(self)

    def count(self):
        return len(self)

    def order_by(self, *args):
        return self


def _run_due_sweep(proc, related_items, outcomes):
    """Drive the real process_due_messages() over exactly one group."""
    related = _RelatedQuerySet(related_items)
    due_entry = related_items[0]
    attempted = []

    def fake_send(m):
        attempted.append(m.id)
        return outcomes.get(m.id, SendOutcome.SENT)

    with patch(
        'bulkcampaign_processor.services.bulk_campaign_processor.reconcile_stale_send_cap_claims',
        return_value=0,
    ), patch(
        'bulkcampaign_processor.services.bulk_campaign_processor.BulkCampaignMessage'
    ) as mock_model, patch.object(
        proc, '_send_message', side_effect=fake_send
    ), patch.object(
        proc.validator, 'validate_message_pair', return_value=ValidationOutcome.VALID
    ):
        chain = mock_model.objects.filter.return_value.select_related.return_value
        # First order_by() is the due sweep; every later one is the group's messages.
        chain.order_by.side_effect = [[due_entry]] + [related] * 12
        proc.message_group = MagicMock()
        processed = proc.process_due_messages()

    return attempted, processed, related


@pytest.mark.django_db
def test_second_overdue_step_is_held_for_the_next_sweep():
    """The exact shape that sent steps 142 and 143 one second apart."""
    proc = BulkCampaignProcessor()

    attempted, processed, _ = _run_due_sweep(proc, [_msg(142), _msg(143)], {})

    assert attempted == [142], 'only one regular message may be delivered per sweep'
    assert processed == 1


@pytest.mark.django_db
def test_opt_out_notice_still_rides_along():
    """The notice accompanies its regular message; it is not a separate touch."""
    proc = BulkCampaignProcessor()

    attempted, processed, _ = _run_due_sweep(
        proc, [_msg(142), _msg(143), _msg(500, 'opt_out_notice')], {}
    )

    assert attempted == [142, 500]
    assert processed == 2


@pytest.mark.django_db
def test_a_replay_does_not_consume_the_sweeps_slot():
    """Otherwise a group whose earlier step is already sent would never progress."""
    proc = BulkCampaignProcessor()

    attempted, processed, _ = _run_due_sweep(
        proc,
        [_msg(142, status='sent'), _msg(143)],
        {142: SendOutcome.ALREADY_SENT},
    )

    assert attempted == [142, 143], 'the already-sent step must not block the next one'
    assert processed == 1, 'a replay is not a delivery'


@pytest.mark.django_db
def test_deferral_stops_the_group_without_failing_it():
    proc = BulkCampaignProcessor()

    attempted, processed, related = _run_due_sweep(
        proc, [_msg(142), _msg(143)], {142: SendOutcome.DEFERRED}
    )

    assert attempted == [142]
    assert processed == 0
    assert related.updated_with == [], 'a deferral must not rewrite the group'


@pytest.mark.django_db
def test_failure_still_marks_the_group_failed():
    proc = BulkCampaignProcessor()

    _, _, related = _run_due_sweep(
        proc, [_msg(142), _msg(143)], {142: SendOutcome.FAILED}
    )

    assert related.updated_with, 'a real failure must still brand the group'
    assert related.updated_with[0]['status'] == 'failed'


def test_already_sent_is_a_distinct_outcome():
    """Folding it into SENT would either stall the group or miscount a skip."""
    assert SendOutcome.ALREADY_SENT is not SendOutcome.SENT
    assert SendOutcome.ALREADY_SENT.value == 'already_sent'
