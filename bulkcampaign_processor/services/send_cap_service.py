"""Atomic send-cap claim / refund for nurturing bulk (drip / reminder / blast) dispatch."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from acs.models.send_caps import NurturingCampaignSendBucket, NurturingCampaignSendCap
from external_models.models.nurturing_campaigns import BulkCampaignMessage

if TYPE_CHECKING:
    from external_models.models.nurturing_campaigns import LeadNurturingCampaign

logger = logging.getLogger(__name__)

OPT_OUT_MESSAGE_TYPES = frozenset({'opt_out_notice', 'opt_out_confirmation'})
COUNTED_BULK_STATUSES = ('sent', 'delivered', 'opened', 'clicked', 'replied')


@dataclass(frozen=True)
class ClaimResult:
    allowed: bool
    blocking_cap_id: int | None
    blocking_cap_period: str | None
    next_reset_at: datetime | None
    bucket_ids: tuple[int, ...]
    rolling_cap_ids: tuple[int, ...]
    claim_token: str | None
    claimed_at: datetime | None
    enforcement_skipped: bool = False


def counted_bulk_statuses() -> tuple[str, ...]:
    return COUNTED_BULK_STATUSES


def _channel_q(channel: str | None) -> Q:
    return Q(channel=channel) | Q(channel__isnull=True)


def try_claim_send_slot(
    *,
    campaign: 'LeadNurturingCampaign',
    channel: str | None,
    message_type: str,
    now: datetime | None = None,
) -> ClaimResult:
    """Return claim outcome for one outbound bulk message row.

    Calendar caps: lock bucket rows, increment on success inside one transaction.
    Rolling caps: aggregate ``BulkCampaignMessage.sent_at`` only (no bucket rows).
    """
    if not getattr(settings, 'SEND_CAPS_ENFORCEMENT_ENABLED', True):
        return ClaimResult(
            allowed=True,
            blocking_cap_id=None,
            blocking_cap_period=None,
            next_reset_at=None,
            bucket_ids=(),
            rolling_cap_ids=(),
            claim_token=None,
            claimed_at=None,
            enforcement_skipped=True,
        )

    if message_type in OPT_OUT_MESSAGE_TYPES:
        return ClaimResult(
            allowed=True,
            blocking_cap_id=None,
            blocking_cap_period=None,
            next_reset_at=None,
            bucket_ids=(),
            rolling_cap_ids=(),
            claim_token=None,
            claimed_at=None,
        )

    if now is None:
        now = timezone.now()

    caps = list(
        NurturingCampaignSendCap.objects.filter(campaign=campaign, is_enabled=True).filter(_channel_q(channel))
    )
    caps = [c for c in caps if message_type in c.effective_counted_message_types()]
    if not caps:
        return ClaimResult(
            allowed=True,
            blocking_cap_id=None,
            blocking_cap_period=None,
            next_reset_at=None,
            bucket_ids=(),
            rolling_cap_ids=(),
            claim_token=None,
            claimed_at=None,
        )

    blocking: list[tuple[NurturingCampaignSendCap, datetime]] = []
    increments: list[NurturingCampaignSendBucket] = []
    rolling_passed: list[int] = []

    with transaction.atomic():
        for cap in caps:
            window_start, window_end, period_key = cap.compute_window(now=now)
            if cap.boundary == NurturingCampaignSendCap.BOUNDARY_CALENDAR:
                bucket, _created = NurturingCampaignSendBucket.objects.get_or_create(
                    cap=cap,
                    period_key=period_key,
                    defaults={
                        'window_start': window_start,
                        'window_end': window_end,
                    },
                )
                bucket = NurturingCampaignSendBucket.objects.select_for_update().get(pk=bucket.pk)
                if bucket.count >= cap.max_messages:
                    blocking.append((cap, window_end))
                else:
                    increments.append(bucket)
            else:
                count = (
                    BulkCampaignMessage.objects.filter(
                        campaign=campaign,
                        status__in=COUNTED_BULK_STATUSES,
                        sent_at__gte=window_start,
                        sent_at__lt=window_end,
                        message_type__in=cap.effective_counted_message_types(),
                    ).count()
                )
                if count >= cap.max_messages:
                    blocking.append((cap, window_end))
                else:
                    rolling_passed.append(cap.id)

        if blocking:
            transaction.set_rollback(True)
            # Nothing can go out until EVERY full cap has reset, so defer to the latest
            # reset, not the earliest. Taking the earliest meant a campaign whose daily cap
            # was spent deferred its whole backlog to the next hourly reset, where every
            # message was loaded, denied by the daily cap again, and pushed one more hour:
            # ~16k GMLR messages re-swept 24 times a day to send nothing.
            blocking.sort(key=lambda item: item[1])
            cap_block, next_reset_at = blocking[-1]
            return ClaimResult(
                allowed=False,
                blocking_cap_id=cap_block.id,
                blocking_cap_period=cap_block.period,
                next_reset_at=next_reset_at,
                bucket_ids=(),
                rolling_cap_ids=tuple(rolling_passed),
                claim_token=None,
                claimed_at=None,
            )

        if not increments:
            return ClaimResult(
                allowed=True,
                blocking_cap_id=None,
                blocking_cap_period=None,
                next_reset_at=None,
                bucket_ids=(),
                rolling_cap_ids=tuple(rolling_passed),
                claim_token=None,
                claimed_at=None,
            )

        claim_token = str(uuid.uuid4())
        claimed_at = timezone.now()
        for bucket in increments:
            bucket.count += 1
            bucket.save(update_fields=['count', 'updated_at'])

        return ClaimResult(
            allowed=True,
            blocking_cap_id=None,
            blocking_cap_period=None,
            next_reset_at=None,
            bucket_ids=tuple(b.id for b in increments),
            rolling_cap_ids=tuple(rolling_passed),
            claim_token=claim_token,
            claimed_at=claimed_at,
        )


def refund_send_slot(bucket_ids: Iterable[int] | None = None, *, claim: ClaimResult | None = None) -> None:
    """Decrement calendar bucket counters (idempotent per bucket)."""
    ids: tuple[int, ...]
    if bucket_ids is not None:
        ids = tuple(bucket_ids)
    elif claim is not None:
        ids = claim.bucket_ids
    else:
        ids = ()
    if not ids:
        return
    with transaction.atomic():
        for bid in ids:
            bucket = NurturingCampaignSendBucket.objects.select_for_update().get(pk=bid)
            if bucket.count > 0:
                bucket.count -= 1
                bucket.save(update_fields=['count', 'updated_at'])


def should_refund_after_send_failure(success: bool, thread_message: Any) -> bool:
    """Default under-send preference: only refund when explicitly enabled."""
    if success:
        return False
    return bool(getattr(settings, 'SEND_CAP_REFUND_WHEN_NO_THREAD_MESSAGE', False)) and thread_message is None


def clear_send_cap_claim_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(metadata or {})
    out.pop('send_cap_claim', None)
    return out


def reconcile_stale_send_cap_claims(*, max_age_seconds: int | None = None) -> int:
    """Mark stuck in-flight claims as failed (no refund by default). Returns rows updated."""
    threshold_s = max_age_seconds if max_age_seconds is not None else int(
        getattr(settings, 'SEND_CAP_CLAIM_STALE_AFTER_SECONDS', 300)
    )
    cutoff = timezone.now() - timedelta(seconds=threshold_s)
    # Only rows that actually carry a claim. Filtering on "has any metadata" matched every
    # cap-deferred message (they all carry metadata['cap_deferral']), so this re-read the
    # same arbitrary 500 of them on each pass and could miss a real stale claim behind them.
    candidates = BulkCampaignMessage.objects.filter(
        status__in=('pending', 'scheduled'),
        provider_message_id__isnull=True,
        metadata__has_key='send_cap_claim',
    )[:500]

    reconciled = 0
    for msg in candidates:
        meta = msg.metadata or {}
        claim_info = meta.get('send_cap_claim')
        if not isinstance(claim_info, dict):
            continue
        token = claim_info.get('claim_token')
        if not token:
            continue
        claimed_raw = claim_info.get('claimed_at')
        if not claimed_raw:
            continue
        try:
            claimed_at = datetime.fromisoformat(str(claimed_raw).replace('Z', '+00:00'))
        except ValueError:
            continue
        if timezone.is_naive(claimed_at):
            claimed_at = timezone.make_aware(claimed_at, timezone.get_current_timezone())
        if claimed_at > cutoff:
            continue

        age_seconds = int((timezone.now() - claimed_at).total_seconds())
        logger.info(
            'send_cap_stale_reconciled bulk_campaign_message_id=%s claim_token=%s age_seconds=%s',
            msg.id,
            token,
            age_seconds,
        )
        msg.metadata = clear_send_cap_claim_metadata(meta)
        msg.save(update_fields=['metadata', 'updated_at'])
        msg.update_status(
            'failed',
            {'error': 'Stale send_cap_claim reconciled (no provider_message_id)'},
        )
        reconciled += 1

    return reconciled
