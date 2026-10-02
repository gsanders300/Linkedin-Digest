import unittest
from unittest.mock import patch

import main


class MainPipelineTests(unittest.TestCase):
    @patch.object(main, "save_seen_ids")
    @patch.object(main, "send_digest_email", return_value=True)
    @patch.object(main, "get_apify_usage_stats", return_value=None)
    @patch.object(main, "scrape_profiles", return_value=([], "Apify failed"))
    @patch.object(main, "load_seen_ids", return_value=set())
    @patch.object(main, "load_profiles", return_value=["https://linkedin.com/in/test/"])
    def test_scrape_error_fails_after_email_attempt(
        self,
        _load_profiles,
        _load_seen_ids,
        _scrape_profiles,
        _usage,
        send_email,
        save_seen,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "Apify failed"):
            main.main()
        send_email.assert_called_once()
        save_seen.assert_not_called()

    @patch.object(main, "save_seen_ids")
    @patch.object(main, "send_digest_email", return_value=False)
    @patch.object(main, "get_apify_usage_stats", return_value=None)
    @patch.object(main, "scrape_profiles", return_value=([], None))
    @patch.object(main, "load_seen_ids", return_value=set())
    @patch.object(main, "load_profiles", return_value=["https://linkedin.com/in/test/"])
    def test_email_failure_fails_without_persisting(
        self,
        _load_profiles,
        _load_seen_ids,
        _scrape_profiles,
        _usage,
        _send_email,
        save_seen,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "Digest email was not sent"):
            main.main()
        save_seen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
