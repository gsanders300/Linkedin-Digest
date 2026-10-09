import os
import time
import unittest
from unittest.mock import MagicMock, patch

import ai_summarizer
from ai_summarizer import _is_retryable


class ApiError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.code = code


class GeminiRetryTests(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        self.assertTrue(_is_retryable(ApiError(429)))
        self.assertTrue(_is_retryable(ApiError(503)))

    def test_authentication_and_bad_request_errors_are_not_retryable(self) -> None:
        self.assertFalse(_is_retryable(ApiError(400)))
        self.assertFalse(_is_retryable(ApiError(401)))
        self.assertFalse(_is_retryable(ApiError(403)))


class GeminiTimeLimitTests(unittest.TestCase):
    def test_client_sets_request_timeout(self) -> None:
        with patch.object(ai_summarizer, "_client", None), patch.dict(
            os.environ, {"GEMINI_API_KEY": "key"}
        ), patch.object(ai_summarizer.genai, "Client") as client_class:
            ai_summarizer._get_client()

        options = client_class.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, ai_summarizer.REQUEST_TIMEOUT_MS)

    def test_expired_deadline_skips_requests_but_returns_posts(self) -> None:
        client = MagicMock()
        posts = [{"id": "1", "text": "Hello"}, {"id": "2", "text": "World"}]
        with patch.object(ai_summarizer, "_get_client", return_value=client):
            result, error = ai_summarizer.summarize_posts(
                posts, deadline=time.monotonic() - 1
            )

        client.models.generate_content.assert_not_called()
        self.assertEqual(
            [post["summary"] for post in result], ["Summary unavailable."] * 2
        )
        self.assertIn("time limit ran out", error or "")

    def test_retry_is_skipped_when_backoff_would_pass_deadline(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = ApiError(503)
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_next_request_at", 0.0
        ), patch.object(ai_summarizer.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 1 attempt"):
                ai_summarizer.summarize_post(
                    {"id": "1", "text": "Hello"}, deadline=time.monotonic() + 0.5
                )

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
