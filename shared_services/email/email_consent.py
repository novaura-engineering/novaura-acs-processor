"""Consent guard for the ACS send path.

Reads the account-scoped EmailConsent table owned by novaura_crm_rest
(communications/models/email_consent.py) and mirrored here as managed=False. Absence of a
row means "no signal, mailable"; suppression is a row in a state other than subscribed.

Writes belong to the CRM. This module only reads.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EmailSuppressed(Exception):
    """The recipient has withdrawn consent, so the send must not happen.

    Distinct from a delivery failure: callers should record this as a skip rather than an
    error, and must not retry it.
    """

    def __init__(self, email: str, state: str, source: str = ''):
        self.email = email
        self.state = state
        self.source = source
        detail = f'{email} is suppressed for email (state={state}'
        if source:
            detail += f', source={source}'
        super().__init__(detail + ')')


def normalize_email(email: str) -> str:
    return (email or '').strip().lower()


def get_consent(account_id: int, email: str):
    from external_models.models.communications import EmailConsent

    normalized = normalize_email(email)
    if not account_id or not normalized:
        return None
    return EmailConsent.objects.filter(
        account_id=account_id, email_normalized=normalized
    ).first()


def is_email_suppressed(account_id: int, email: str) -> bool:
    consent = get_consent(account_id, email)
    return bool(consent and consent.is_suppressed)


def assert_email_sendable(account_id: Optional[int], email: str) -> None:
    """Raise EmailSuppressed when consent has been withdrawn.

    A missing account_id means the caller could not resolve a scope; that is not treated as
    consent, but there is nothing to look up, so the send proceeds.
    """
    if not account_id:
        return
    consent = get_consent(account_id, email)
    if consent and consent.is_suppressed:
        logger.info(
            'email_send_suppressed account_id=%s email=%s state=%s source=%s',
            account_id,
            consent.email_normalized,
            consent.state,
            consent.source,
        )
        raise EmailSuppressed(consent.email_normalized, consent.state, consent.source)
