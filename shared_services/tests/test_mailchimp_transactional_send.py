"""Tests for Mailchimp Transactional (Mandrill) send helpers (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import requests

from django.test import SimpleTestCase


def _ok_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = payload
    return resp


def _error_response(status_code, payload, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.json.return_value = payload
    resp.text = text
    resp.headers = {}
    return resp


class MandrillSendTests(SimpleTestCase):
    def test_send_posts_expected_payload(self):
        from shared_services.email.mailchimp_transactional import (
            MANDRILL_API,
            send_mailchimp_transactional_email,
        )

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'abc-123'}])

        with patch('requests.post', return_value=resp) as post:
            result = send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>Hello</p>',
                text_body='Hello explicit',
                from_email='Sender Name <from@example.com>',
                reply_to='support@example.com',
                tags=['bulk', 'nurture'],
                subaccount='client-a',
            )

        self.assertEqual(result.message_id, 'abc-123')
        self.assertEqual(result.message, 'sent')
        post.assert_called_once()
        ca = post.call_args
        self.assertEqual(ca.args[0], MANDRILL_API)
        body = ca.kwargs['json']
        self.assertEqual(body['key'], 'key-123')
        msg = body['message']
        self.assertEqual(msg['from_email'], 'from@example.com')
        self.assertEqual(msg['from_name'], 'Sender Name')
        self.assertEqual(msg['to'], [{'email': 'to@example.com', 'type': 'to'}])
        self.assertEqual(msg['subject'], 'Hi')
        self.assertEqual(msg['html'], '<p>Hello</p>')
        self.assertEqual(msg['text'], 'Hello explicit')
        self.assertEqual(msg['headers']['Reply-To'], 'support@example.com')
        self.assertEqual(msg['tags'], ['bulk', 'nurture'])
        self.assertEqual(msg['subaccount'], 'client-a')

    def test_bare_from_address_sends_no_from_name(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'queued', '_id': 'q-1'}])

        with patch('requests.post', return_value=resp) as post:
            result = send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
            )

        msg = post.call_args.kwargs['json']['message']
        self.assertEqual(msg['from_email'], 'from@example.com')
        self.assertNotIn('from_name', msg)
        self.assertEqual(result.message, 'queued')

    def test_unparseable_from_header_raises(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        with patch('requests.post') as post:
            with self.assertRaises(ValueError):
                send_mailchimp_transactional_email(
                    api_key='key-123',
                    to_email='to@example.com',
                    subject='Hi',
                    html_body='<p>x</p>',
                    text_body='x',
                    from_email='Nobody',
                )
        post.assert_not_called()

    def test_text_generated_from_html_when_absent(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'id-1'}])

        with patch('requests.post', return_value=resp) as post:
            send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>Hello there</p>',
                text_body=None,
                from_email='from@example.com',
            )

        msg = post.call_args.kwargs['json']['message']
        self.assertEqual(msg['text'], 'Hello there')

    def test_extra_headers_merge_with_reply_to(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'id-2'}])

        with patch('requests.post', return_value=resp) as post:
            send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
                reply_to='support@example.com',
                extra_headers={'List-Unsubscribe': '<https://example.com/u>'},
            )

        headers = post.call_args.kwargs['json']['message']['headers']
        self.assertEqual(headers['List-Unsubscribe'], '<https://example.com/u>')
        self.assertEqual(headers['Reply-To'], 'support@example.com')

    def test_reserved_and_overlong_tags_are_filtered(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'id-3'}])

        with patch('requests.post', return_value=resp) as post:
            send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
                tags=['_reserved', 'ok', 'x' * 80],
            )

        tags = post.call_args.kwargs['json']['message']['tags']
        self.assertNotIn('_reserved', tags)
        self.assertIn('ok', tags)
        self.assertEqual(len(tags[-1]), 50)

    def test_optional_fields_omitted_when_unset(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'id-4'}])

        with patch('requests.post', return_value=resp) as post:
            send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
            )

        msg = post.call_args.kwargs['json']['message']
        for key in ('tags', 'subaccount', 'track_opens', 'track_clicks', 'metadata', 'headers'):
            self.assertNotIn(key, msg)


class MandrillPerRecipientFailureTests(SimpleTestCase):
    """HTTP 200 with a rejected/invalid recipient is a failed send, not a success."""

    def test_rejected_recipient_raises_with_reason(self):
        from shared_services.email.mailchimp_transactional import (
            MandrillSendRejected,
            send_mailchimp_transactional_email,
        )

        resp = _ok_response(
            [
                {
                    'email': 'to@example.com',
                    'status': 'rejected',
                    '_id': 'rej-1',
                    'reject_reason': 'hard-bounce',
                }
            ]
        )

        with patch('requests.post', return_value=resp):
            with self.assertRaises(MandrillSendRejected) as ctx:
                send_mailchimp_transactional_email(
                    api_key='key-123',
                    to_email='to@example.com',
                    subject='Hi',
                    html_body='<p>x</p>',
                    text_body='x',
                    from_email='from@example.com',
                )

        self.assertEqual(ctx.exception.status, 'rejected')
        self.assertEqual(ctx.exception.reject_reason, 'hard-bounce')
        self.assertEqual(ctx.exception.email, 'to@example.com')

    def test_invalid_recipient_raises(self):
        from shared_services.email.mailchimp_transactional import (
            MandrillSendRejected,
            send_mailchimp_transactional_email,
        )

        resp = _ok_response([{'email': 'to@example.com', 'status': 'invalid', '_id': None}])

        with patch('requests.post', return_value=resp):
            with self.assertRaises(MandrillSendRejected):
                send_mailchimp_transactional_email(
                    api_key='key-123',
                    to_email='to@example.com',
                    subject='Hi',
                    html_body='<p>x</p>',
                    text_body='x',
                    from_email='from@example.com',
                )

    def test_picks_matching_recipient_from_multi_entry_response(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response(
            [
                {'email': 'other@example.com', 'status': 'rejected', 'reject_reason': 'unsub'},
                {'email': 'To@Example.com', 'status': 'sent', '_id': 'mine-1'},
            ]
        )

        with patch('requests.post', return_value=resp):
            result = send_mailchimp_transactional_email(
                api_key='key-123',
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
            )

        self.assertEqual(result.message_id, 'mine-1')
        self.assertEqual(result.message, 'sent')

    def test_unrecognized_status_raises(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _ok_response([{'email': 'to@example.com', 'status': 'weird', '_id': 'x'}])

        with patch('requests.post', return_value=resp):
            with self.assertRaises(ValueError):
                send_mailchimp_transactional_email(
                    api_key='key-123',
                    to_email='to@example.com',
                    subject='Hi',
                    html_body='<p>x</p>',
                    text_body='x',
                    from_email='from@example.com',
                )


class MandrillRetryTests(SimpleTestCase):
    def test_permanent_error_is_not_retried(self):
        """Invalid_Key arrived as HTTP 500 before Feb 2023; retrying it never helps."""
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _error_response(
            500,
            {'status': 'error', 'code': -1, 'name': 'Invalid_Key', 'message': 'Invalid API key'},
            text='{"name": "Invalid_Key"}',
        )

        with patch('shared_services.email.mailchimp_transactional.time.sleep'):
            with patch('requests.post', return_value=resp) as post:
                with self.assertRaises(requests.HTTPError):
                    send_mailchimp_transactional_email(
                        api_key='bad-key',
                        to_email='to@example.com',
                        subject='Hi',
                        html_body='<p>x</p>',
                        text_body='x',
                        from_email='from@example.com',
                    )

        self.assertEqual(post.call_count, 1)

    def test_transient_5xx_is_retried_then_succeeds(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        transient = _error_response(503, {}, text='upstream unavailable')
        ok = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'after-retry'}])

        with patch('shared_services.email.mailchimp_transactional.time.sleep'):
            with patch('requests.post', side_effect=[transient, ok]) as post:
                result = send_mailchimp_transactional_email(
                    api_key='key-123',
                    to_email='to@example.com',
                    subject='Hi',
                    html_body='<p>x</p>',
                    text_body='x',
                    from_email='from@example.com',
                )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(result.message_id, 'after-retry')

    def test_unauthorized_is_not_retried(self):
        from shared_services.email.mailchimp_transactional import send_mailchimp_transactional_email

        resp = _error_response(401, {'name': 'Invalid_Key', 'message': 'Invalid API key'})

        with patch('shared_services.email.mailchimp_transactional.time.sleep'):
            with patch('requests.post', return_value=resp) as post:
                with self.assertRaises(requests.HTTPError):
                    send_mailchimp_transactional_email(
                        api_key='bad-key',
                        to_email='to@example.com',
                        subject='Hi',
                        html_body='<p>x</p>',
                        text_body='x',
                        from_email='from@example.com',
                    )

        self.assertEqual(post.call_count, 1)


class MandrillAdapterTests(SimpleTestCase):
    def test_adapter_reads_config_and_credentials(self):
        from shared_services.email.mailchimp_transactional import (
            MailchimpTransactionalEmailAdapter,
        )

        resp = _ok_response([{'email': 'to@example.com', 'status': 'sent', '_id': 'cfg-1'}])

        with patch('requests.post', return_value=resp) as post:
            result = MailchimpTransactionalEmailAdapter().send(
                credentials={'api_key': 'secret-key'},
                config={
                    'subaccount': 'client-a',
                    'track_opens': True,
                    'track_clicks': False,
                    'metadata': {'account_id': 12},
                },
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
                reply_to=None,
            )

        body = post.call_args.kwargs['json']
        self.assertEqual(body['key'], 'secret-key')
        msg = body['message']
        self.assertEqual(msg['subaccount'], 'client-a')
        self.assertIs(msg['track_opens'], True)
        self.assertIs(msg['track_clicks'], False)
        self.assertEqual(msg['metadata'], {'account_id': '12'})
        self.assertEqual(result.message_id, 'cfg-1')

    def test_adapter_requires_api_key(self):
        from shared_services.email.mailchimp_transactional import (
            MailchimpTransactionalEmailAdapter,
        )

        with self.assertRaises(ValueError):
            MailchimpTransactionalEmailAdapter().send(
                credentials={},
                config={},
                to_email='to@example.com',
                subject='Hi',
                html_body='<p>x</p>',
                text_body='x',
                from_email='from@example.com',
                reply_to=None,
            )

    def test_registry_resolves_adapter(self):
        from shared_services.email.mailchimp_transactional import (
            MailchimpTransactionalEmailAdapter,
        )
        from shared_services.email.registry import get_email_provider_adapter

        adapter = get_email_provider_adapter('mailchimp_transactional')
        self.assertIsInstance(adapter, MailchimpTransactionalEmailAdapter)
        self.assertEqual(adapter.provider_name, 'mailchimp_transactional')
