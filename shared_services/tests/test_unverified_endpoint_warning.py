"""The unverified-endpoint warning is rate-limited to once per sweep.

`ContactEndpoint.is_verified` defaults to False and nothing ever sets it True, so this
branch is taken for every message validated. Logging it per message made it ~92% of the
bulk worker's CloudWatch volume.
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from shared_services.message_validation_service import MessageValidationService


class UnverifiedEndpointWarningTests(SimpleTestCase):
    def setUp(self):
        self.service = MessageValidationService(MagicMock())

    def test_warns_once_per_endpoint_per_sweep(self):
        with self.assertLogs('shared_services.message_validation_service', level='WARNING') as logs:
            for _ in range(50):
                self.service._warn_unverified_once('noreply@example.com')

        self.assertEqual(len(logs.records), 1)
        self.assertIn('noreply@example.com', logs.output[0])

    def test_distinct_endpoints_each_warn(self):
        with self.assertLogs('shared_services.message_validation_service', level='WARNING') as logs:
            self.service._warn_unverified_once('a@example.com')
            self.service._warn_unverified_once('b@example.com')
            self.service._warn_unverified_once('a@example.com')

        self.assertEqual(len(logs.records), 2)

    def test_a_new_sweep_warns_again(self):
        """The service is rebuilt per run, so a later sweep still reports the problem."""
        with self.assertLogs('shared_services.message_validation_service', level='WARNING') as logs:
            self.service._warn_unverified_once('noreply@example.com')
            MessageValidationService(MagicMock())._warn_unverified_once('noreply@example.com')

        self.assertEqual(len(logs.records), 2)

    def test_message_says_it_does_not_block_sending(self):
        """The old wording read like a delivery failure; it is not one."""
        with self.assertLogs('shared_services.message_validation_service', level='WARNING') as logs:
            self.service._warn_unverified_once('noreply@example.com')

        self.assertIn('does not block sending', logs.output[0])
