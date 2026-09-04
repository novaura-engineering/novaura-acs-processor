"""Resolve email provider adapter by ContactEndpointEmailSettings.provider value."""

from __future__ import annotations

from shared_services.email.base import EmailProviderAdapter

_PROVIDER_MAILGUN = 'mailgun'
_PROVIDER_POSTMARK = 'postmark'
_PROVIDER_MAILCHIMP_TRANSACTIONAL = 'mailchimp_transactional'


def get_email_provider_adapter(provider: str) -> EmailProviderAdapter:
    if provider == _PROVIDER_MAILGUN:
        from shared_services.email.mailgun import MailgunEmailAdapter

        return MailgunEmailAdapter()
    if provider == _PROVIDER_POSTMARK:
        from shared_services.email.postmark import PostmarkEmailAdapter

        return PostmarkEmailAdapter()
    if provider == _PROVIDER_MAILCHIMP_TRANSACTIONAL:
        from shared_services.email.mailchimp_transactional import (
            MailchimpTransactionalEmailAdapter,
        )

        return MailchimpTransactionalEmailAdapter()
    raise ValueError(f'Unknown or unsupported email provider: {provider!r}')
