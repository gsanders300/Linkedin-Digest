import os
import unittest
from unittest.mock import MagicMock, patch

import email_sender


class _FakeSmtp:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def login(self, sender: str, password: str) -> None:
        return None

    def send_message(self, message) -> None:
        return None


class EmailSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "GMAIL_ADDRESS": "sender@example.com",
                "GMAIL_APP_PASSWORD": "password",
                "RECIPIENT_EMAIL": "recipient@example.com",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_smtp_connection_uses_timeout(self) -> None:
        with patch.object(
            email_sender.smtplib, "SMTP_SSL", return_value=_FakeSmtp()
        ) as smtp:
            self.assertTrue(email_sender.send_digest_email([]))

        smtp.assert_called_once_with(
            "smtp.gmail.com", 465, timeout=email_sender.SMTP_TIMEOUT_SECONDS
        )

    def test_transient_connection_failure_is_retried(self) -> None:
        smtp = MagicMock(side_effect=[OSError("temporary"), _FakeSmtp()])
        with patch.object(email_sender.smtplib, "SMTP_SSL", smtp), patch.object(
            email_sender.time, "sleep"
        ) as sleep:
            self.assertTrue(email_sender.send_digest_email([]))

        self.assertEqual(smtp.call_count, 2)
        sleep.assert_called_once_with(email_sender.SMTP_RETRY_BASE_DELAY)


if __name__ == "__main__":
    unittest.main()
