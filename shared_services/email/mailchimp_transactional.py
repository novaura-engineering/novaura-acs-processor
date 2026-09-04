"""Mailchimp Transactional (Mandrill) HTTP send (messages/send only)."""

from __future__ import annotations

import logging
import random
import time
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from shared_services.email.base import EmailProviderAdapter, EmailSendResult
from shared_services.email.mailgun import html_to_plain_text

logger = logging.getLogger(__name__)

# Bounded retries for transient Mandrill/network failures.
# Retrying after timeout can duplicate sends if the server accepted the request; keep attempts low.
MANDRILL_POST_MAX_ATTEMPTS = 3
MANDRILL_POST_BASE_DELAY_SEC = 0.5
MANDRILL_POST_MAX_BACKOFF_SEC = 8.0

MANDRILL_API = 'https://mandrillapp.com/api/1.0/messages/send.json'

# Per-recipient statuses that mean Mandrill accepted the message for delivery.
MANDRILL_ACCEPTED_STATUSES = frozenset({'sent', 'queued', 'scheduled'})
# Per-recipient statuses that mean the send failed even though HTTP was 200.
MANDRILL_FAILED_STATUSES = frozenset({'rejected', 'invalid'})

# Mandrill returns these under a non-2xx status but they will never succeed on retry.
# Invalid keys returned HTTP 500 until Feb 2023 and 401 since, so we match on the error
# name rather than trusting the status code to separate permanent from transient.
MANDRILL_PERMANENT_ERROR_NAMES = frozenset(
    {
        'Invalid_Key',
        'PaymentRequired',
        'ValidationError',
        'GeneralError',
        'Unknown_Subaccount',
        'Unknown_Template',
        'Invalid_Tag_Name',
    }
)

# Mandrill rejects tags that start with an underscore and truncates past 50 chars.
MANDRILL_TAG_MAX_LEN = 50
MANDRILL_MAX_TAGS = 10


class MandrillSendRejected(RuntimeError):
    """HTTP 200 from Mandrill, but the recipient was rejected or invalid.

    Mandrill reports per-recipient failures inside a success response, so this is
    raised to give callers the same failure signal other providers give via HTTP.
    """

    def __init__(self, email: str, status: str, reject_reason: Optional[str]):
        self.email = email
        self.status = status
        self.reject_reason = reject_reason
        detail = f'Mandrill rejected {email}: status={status}'
        if reject_reason:
            detail += f' reject_reason={reject_reason}'
        super().__init__(detail)


def _resolve_api_key(credentials: Dict[str, Any]) -> str:
    key = (
        (credentials or {}).get('api_key')
        or (credentials or {}).get('MANDRILL_API_KEY')
        or (credentials or {}).get('MAILCHIMP_TRANSACTIONAL_API_KEY')
    )
    if not key or not isinstance(key, str):
        raise ValueError('Mailchimp Transactional credentials missing api_key')
    return key.strip()


def _split_from_header(from_email: str) -> Tuple[str, Optional[str]]:
    """Split ``"Name <addr@example.com>"`` into Mandrill's separate from_email / from_name.

    Mailgun and Postmark accept a combined RFC 5322 From header, but Mandrill takes the
    address and display name as distinct fields, so the combined form our dispatch layer
    builds has to be parsed apart here.
    """
    name, addr = parseaddr(from_email or '')
    # parseaddr does not validate: it returns bare text unchanged as the address, so a
    # malformed header would otherwise reach Mandrill as a from_email it will reject.
    if not addr or '@' not in addr:
        raise ValueError(f'Could not parse a from address out of {from_email!r}')
    return addr, (name.strip() or None)


def _clean_tags(tags: Optional[List[str]]) -> List[str]:
    """Drop tags Mandrill reserves (leading underscore), truncate, and cap the count."""
    if not tags:
        return []
    clean: List[str] = []
    for tag in tags:
        if not tag or not isinstance(tag, str):
            continue
        value = tag.strip()
        if not value or value.startswith('_'):
            continue
        clean.append(value[:MANDRILL_TAG_MAX_LEN])
        if len(clean) >= MANDRILL_MAX_TAGS:
            break
    return clean


def _clean_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    clean: Dict[str, str] = {}
    for key, value in metadata.items():
        if key is None or value is None:
            continue
        key_str = str(key).strip()
        if not key_str:
            continue
        clean[key_str] = str(value)
    return clean


def _mandrill_error_payload(response) -> Tuple[Optional[str], str]:
    """Return (error name, message) from a Mandrill error body."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    name = payload.get('name')
    message = payload.get('message') or (getattr(response, 'text', '') or '')[:500]
    return (str(name) if name else None), message


def _is_permanent_mandrill_error(response) -> bool:
    name, _ = _mandrill_error_payload(response)
    return bool(name and name in MANDRILL_PERMANENT_ERROR_NAMES)


def _raise_mandrill_http_error(response) -> None:
    import requests

    name, message = _mandrill_error_payload(response)
    detail = f'Mandrill {response.status_code} name={name} message={message}'
    raise requests.HTTPError(detail, response=response)


def _retry_after_seconds(response) -> Optional[float]:
    headers = getattr(response, 'headers', None) or {}
    raw = headers.get('Retry-After')
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _backoff_sleep(attempt: int, response=None) -> None:
    """Exponential backoff with jitter; honors Retry-After on 429 when parseable."""
    if response is not None and response.status_code == 429:
        retry_after = _retry_after_seconds(response)
        if retry_after is not None and retry_after >= 0:
            time.sleep(min(MANDRILL_POST_MAX_BACKOFF_SEC, retry_after + random.uniform(0, 0.5)))
            return
    base = min(
        MANDRILL_POST_MAX_BACKOFF_SEC,
        MANDRILL_POST_BASE_DELAY_SEC * (2**attempt),
    )
    time.sleep(base + random.uniform(0, min(1.0, base * 0.25)))


def _post_mandrill_message(*, body: Dict[str, Any], timeout: int) -> Any:
    """POST with retries on timeout, connection errors, transient 5xx, and 429."""
    import requests
    from requests import RequestException

    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

    for attempt in range(MANDRILL_POST_MAX_ATTEMPTS):
        try:
            resp = requests.post(MANDRILL_API, headers=headers, json=body, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                # A named permanent error (e.g. Invalid_Key, which predates Mandrill's
                # switch to 401) will fail identically on every retry.
                if attempt < MANDRILL_POST_MAX_ATTEMPTS - 1 and not _is_permanent_mandrill_error(resp):
                    logger.warning(
                        'mandrill_post_retry status=%s attempt=%s/%s url=%s',
                        resp.status_code,
                        attempt + 1,
                        MANDRILL_POST_MAX_ATTEMPTS,
                        MANDRILL_API,
                    )
                    _backoff_sleep(attempt, resp)
                    continue
            if not resp.ok:
                _raise_mandrill_http_error(resp)
            return resp
        except requests.Timeout:
            if attempt < MANDRILL_POST_MAX_ATTEMPTS - 1:
                logger.warning(
                    'mandrill_post_retry timeout attempt=%s/%s url=%s',
                    attempt + 1,
                    MANDRILL_POST_MAX_ATTEMPTS,
                    MANDRILL_API,
                )
                _backoff_sleep(attempt, None)
                continue
            raise
        except requests.ConnectionError:
            if attempt < MANDRILL_POST_MAX_ATTEMPTS - 1:
                logger.warning(
                    'mandrill_post_retry connection_error attempt=%s/%s url=%s',
                    attempt + 1,
                    MANDRILL_POST_MAX_ATTEMPTS,
                    MANDRILL_API,
                )
                _backoff_sleep(attempt, None)
                continue
            raise
        except RequestException:
            raise
    raise RuntimeError('mandrill_post_retry exhausted without response')


def _result_for_recipient(payload: Any, to_email: str) -> Dict[str, Any]:
    """Pick our recipient's entry out of Mandrill's per-recipient result array."""
    if not isinstance(payload, list) or not payload:
        raise ValueError(f'Unexpected Mandrill response shape: {payload!r}')
    target = (to_email or '').strip().lower()
    for entry in payload:
        if isinstance(entry, dict) and str(entry.get('email', '')).strip().lower() == target:
            return entry
    first = payload[0]
    if not isinstance(first, dict):
        raise ValueError(f'Unexpected Mandrill response entry: {first!r}')
    return first


def send_mailchimp_transactional_email(
    *,
    api_key: str,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str],
    from_email: str,
    reply_to: Optional[str] = None,
    tags: Optional[List[str]] = None,
    subaccount: Optional[str] = None,
    track_opens: Optional[bool] = None,
    track_clicks: Optional[bool] = None,
    metadata: Optional[Dict[str, Any]] = None,
    log_context: Optional[Dict[str, Any]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> EmailSendResult:
    try:
        from requests import HTTPError
    except ImportError as e:
        raise ImportError('requests is required for Mailchimp Transactional') from e

    if text_body is not None and text_body.strip():
        text = text_body
    elif html_body.strip():
        text = html_to_plain_text(html_body)
    else:
        text = ''

    from_addr, from_name = _split_from_header(from_email)

    message: Dict[str, Any] = {
        'from_email': from_addr,
        'to': [{'email': to_email, 'type': 'to'}],
        'subject': subject,
        'html': html_body,
    }
    if from_name:
        message['from_name'] = from_name
    if text:
        message['text'] = text

    headers: Dict[str, str] = {}
    if extra_headers:
        for name, value in extra_headers.items():
            if name and value:
                headers[str(name)] = str(value)
    if reply_to:
        # Mandrill has no top-level reply_to; it is set as a message header.
        headers['Reply-To'] = reply_to
    if headers:
        message['headers'] = headers

    tag_values = _clean_tags(tags)
    if tag_values:
        message['tags'] = tag_values
    if subaccount:
        message['subaccount'] = subaccount
    if track_opens is not None:
        message['track_opens'] = bool(track_opens)
    if track_clicks is not None:
        message['track_clicks'] = bool(track_clicks)

    clean_metadata = _clean_metadata(metadata)
    if clean_metadata:
        message['metadata'] = clean_metadata

    body = {'key': api_key, 'message': message}

    ctx = log_context or {}
    try:
        resp = _post_mandrill_message(body=body, timeout=30)
        payload = resp.json()
        entry = _result_for_recipient(payload, to_email)
        status = str(entry.get('status') or '').strip().lower()
        message_id_raw = entry.get('_id')
        message_id = str(message_id_raw) if message_id_raw is not None else None

        if status in MANDRILL_FAILED_STATUSES:
            reject_reason = entry.get('reject_reason')
            logger.error(
                'mandrill_send_fail status=%s reject_reason=%s mandrill_message_id=%s '
                'contact_endpoint_id=%s nurturing_campaign_id=%s bulk_campaign_message_id=%s '
                'send_idempotency_key=%s',
                status,
                reject_reason,
                message_id,
                ctx.get('contact_endpoint_id'),
                ctx.get('nurturing_campaign_id'),
                ctx.get('bulk_campaign_message_id'),
                ctx.get('send_idempotency_key'),
            )
            raise MandrillSendRejected(to_email, status, reject_reason)

        if status not in MANDRILL_ACCEPTED_STATUSES:
            raise ValueError(f'Unrecognized Mandrill send status {status!r} for {to_email}')

        logger.info(
            'mandrill_send_ok status=%s mandrill_message_id=%s contact_endpoint_id=%s '
            'nurturing_campaign_id=%s bulk_campaign_message_id=%s send_idempotency_key=%s',
            status,
            message_id,
            ctx.get('contact_endpoint_id'),
            ctx.get('nurturing_campaign_id'),
            ctx.get('bulk_campaign_message_id'),
            ctx.get('send_idempotency_key'),
        )
        return EmailSendResult(
            message_id=message_id,
            message=status,
            raw_response={'result': payload},
        )
    except HTTPError as e:
        response = e.response
        status_code = response.status_code if response is not None else None
        error_name = None
        body_preview = ''
        if response is not None:
            error_name, _ = _mandrill_error_payload(response)
            text_preview = getattr(response, 'text', '') or ''
            body_preview = (text_preview[:500] + '...') if len(text_preview) > 500 else text_preview
        logger.error(
            'mandrill_send_fail http_status=%s name=%s contact_endpoint_id=%s '
            'nurturing_campaign_id=%s bulk_campaign_message_id=%s send_idempotency_key=%s '
            'error=%s body_preview=%s',
            status_code,
            error_name,
            ctx.get('contact_endpoint_id'),
            ctx.get('nurturing_campaign_id'),
            ctx.get('bulk_campaign_message_id'),
            ctx.get('send_idempotency_key'),
            e,
            body_preview,
        )
        raise
    except MandrillSendRejected:
        raise
    except Exception as e:
        logger.error(
            'mandrill_send_fail contact_endpoint_id=%s nurturing_campaign_id=%s '
            'bulk_campaign_message_id=%s send_idempotency_key=%s error=%s',
            ctx.get('contact_endpoint_id'),
            ctx.get('nurturing_campaign_id'),
            ctx.get('bulk_campaign_message_id'),
            ctx.get('send_idempotency_key'),
            e,
        )
        raise


class MailchimpTransactionalEmailAdapter(EmailProviderAdapter):
    provider_name = 'mailchimp_transactional'

    def send(
        self,
        *,
        credentials: Dict[str, Any],
        config: Dict[str, Any],
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str],
        from_email: str,
        reply_to: Optional[str] = None,
        tags: Optional[List[str]] = None,
        log_context: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> EmailSendResult:
        api_key = _resolve_api_key(credentials)
        cfg = config or {}
        subaccount = cfg.get('subaccount')
        subaccount = subaccount.strip() if isinstance(subaccount, str) else None
        raw_track_opens = cfg.get('track_opens')
        track_opens = raw_track_opens if isinstance(raw_track_opens, bool) else None
        raw_track_clicks = cfg.get('track_clicks')
        track_clicks = raw_track_clicks if isinstance(raw_track_clicks, bool) else None
        metadata = cfg.get('metadata') if isinstance(cfg.get('metadata'), dict) else None
        return send_mailchimp_transactional_email(
            api_key=api_key,
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            from_email=from_email,
            reply_to=reply_to,
            tags=tags,
            subaccount=subaccount or None,
            track_opens=track_opens,
            track_clicks=track_clicks,
            metadata=metadata,
            log_context=log_context,
            extra_headers=extra_headers,
        )
