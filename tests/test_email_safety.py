import unittest
from unittest.mock import patch

from core.config import Config
from core.reporter import Reporter


class EmailSafetyTest(unittest.TestCase):
    def test_email_is_disabled_by_default(self):
        with patch.object(Config, "EMAIL_ENABLED", False), patch(
            "core.reporter.smtplib.SMTP_SSL"
        ) as smtp:
            sent = Reporter().send_email("test", "content")

        self.assertFalse(sent)
        smtp.assert_not_called()

    def test_incomplete_recipient_configuration_is_rejected(self):
        with patch.object(Config, "EMAIL_ENABLED", True), patch.object(
            Config, "EMAIL_USER", "sender@example.com"
        ), patch.object(Config, "EMAIL_PWD", "secret"), patch.object(
            Config, "EMAIL_TO", []
        ), patch("core.reporter.smtplib.SMTP_SSL") as smtp:
            sent = Reporter().send_email("test", "content")

        self.assertFalse(sent)
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
