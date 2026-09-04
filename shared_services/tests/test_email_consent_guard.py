"""Consent guard on the ACS send path (no DB; consent lookups are mocked)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from shared_services.email.email_consent import (
    EmailSuppressed,
    assert_email_sendable,
    is_email_suppressed,
    normalize_email,
)


def _consent(state, source='mailchimp', email='blocked@example.com'):
    """Stand-in for the mirrored EmailConsent row, with the same is_suppressed rule."""
    return SimpleNamespace(
        email_normalized=email,
        state=state,
        source=source,
        is_suppressed=state != 'subscribed',
    )


class NormalizeEmailTests(SimpleTestCase):
    def test_trims_and_lowercases(self):
        self.assertEqual(normalize_email('  Foo@Example.COM '), 'foo@example.com')
        self.assertEqual(normalize_email(None), '')


class AssertEmailSendableTests(SimpleTestCase):
    def test_raises_for_each_suppressed_state(self):
        for state in ('unsubscribed', 'cleaned', 'complained'):
            with patch(
                'shared_services.email.email_consent.get_consent',
                return_value=_consent(state),
            ):
                with self.assertRaises(EmailSuppressed) as ctx:
                    assert_email_sendable(1, 'blocked@example.com')
                self.assertEqual(ctx.exception.state, state)

    def test_passes_for_subscribed(self):
        with patch(
            'shared_services.email.email_consent.get_consent',
            return_value=_consent('subscribed'),
        ):
            assert_email_sendable(1, 'ok@example.com')

    def test_passes_when_no_row(self):
        with patch('shared_services.email.email_consent.get_consent', return_value=None):
            assert_email_sendable(1, 'unknown@example.com')

    def test_missing_account_skips_lookup(self):
        with patch('shared_services.email.email_consent.get_consent') as get_consent:
            assert_email_sendable(None, 'anything@example.com')
        get_consent.assert_not_called()

    def test_is_email_suppressed_reflects_state(self):
        with patch(
            'shared_services.email.email_consent.get_consent',
            return_value=_consent('unsubscribed'),
        ):
            self.assertTrue(is_email_suppressed(1, 'blocked@example.com'))
        with patch(
            'shared_services.email.email_consent.get_consent',
            return_value=_consent('subscribed'),
        ):
            self.assertFalse(is_email_suppressed(1, 'ok@example.com'))


class DispatchGuardTests(SimpleTestCase):
    """The guard must sit in front of the provider, not after it."""

    def _endpoint(self):
        endpoint = MagicMock()
        endpoint.account_id = 1
        endpoint.value = 'noreply@example.com'
        endpoint.channels.filter.return_value.exists.return_value = True
        return endpoint

    def _send(self, endpoint, to_email):
        from shared_services.email.email_dispatch import send_from_contact_endpoint

        return send_from_contact_endpoint(
            endpoint,
            to_email=to_email,
            subject='Hi',
            html_body='<p>x</p>',
        )

    @patch('shared_services.email.email_dispatch.get_email_provider_adapter')
    @patch('shared_services.email.email_dispatch.load_credentials_for_email_settings')
    def test_suppressed_recipient_never_reaches_provider(self, load_creds, get_adapter):
        with patch(
            'shared_services.email.email_consent.get_consent',
            return_value=_consent('unsubscribed'),
        ):
            with self.assertRaises(EmailSuppressed):
                self._send(self._endpoint(), 'blocked@example.com')
        get_adapter.assert_not_called()
        load_creds.assert_not_called()

    @patch('shared_services.email.email_dispatch.get_email_provider_adapter')
    @patch('shared_services.email.email_dispatch.load_credentials_for_email_settings')
    def test_unsuppressed_recipient_reaches_provider(self, load_creds, get_adapter):
        load_creds.return_value = {'api_key': 'k'}
        with patch('shared_services.email.email_consent.get_consent', return_value=None):
            self._send(self._endpoint(), 'allowed@example.com')
        get_adapter.assert_called_once()
