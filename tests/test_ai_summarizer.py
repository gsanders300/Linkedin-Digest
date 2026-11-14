import json
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

    def test_503_spike_recovers_within_retry_budget(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [ApiError(503)] * 3 + [
            MagicMock(text="Summary.")
        ]
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep") as sleep:
            summary = ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(summary, "Summary.")
        backoffs = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(backoffs), 3)
        self.assertGreaterEqual(backoffs[-1], 20.0)

    def test_rate_limit_failure_explains_how_to_fix(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = ApiError(429)
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Lower REQUESTS_PER_MINUTE"):
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

    def test_daily_quota_429_skips_retries_and_explains(self) -> None:
        client = MagicMock()
        error = ApiError(429)
        error.args = ("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",)
        client.models.generate_content.side_effect = error
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Daily quota used up"):
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(client.models.generate_content.call_count, 2)  # 1 per model
        sleep.assert_not_called()

    def test_posts_are_batched_and_unicode_survives(self) -> None:
        summary = "Isn’t “this” great 🚀."
        posts = [{"id": str(i), "text": f"Post {i}"} for i in range(12)]

        def respond(model, contents, config):
            count = contents.count("=== Post ")
            return MagicMock(text=json.dumps({str(n): summary for n in range(1, count + 1)}))

        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual(client.models.generate_content.call_count, 2)  # 10 + 2
        self.assertEqual([post["summary"] for post in result], [summary] * 12)

    def test_post_missing_from_batch_is_summarized_alone(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]

        def respond(model, contents, config):
            if config is ai_summarizer._JSON_CONFIG:
                return MagicMock(text='{"1": "Batch summary.", "9": "Unknown post."}')
            return MagicMock(text="Single summary.")

        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual(
            [post["summary"] for post in result], ["Batch summary.", "Single summary."]
        )
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_invalid_batch_json_falls_back_to_one_request_per_post(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]

        def respond(model, contents, config):
            if config is ai_summarizer._JSON_CONFIG:
                return MagicMock(text='{"1": "cut off')
            return MagicMock(text="Single summary.")

        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual([post["summary"] for post in result], ["Single summary."] * 2)
        self.assertEqual(client.models.generate_content.call_count, 3)  # 1 batch + 2 single

    def test_server_error_failure_has_no_rate_limit_hint(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = ApiError(503)
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertNotIn("REQUESTS_PER_MINUTE", str(ctx.exception))
        self.assertEqual(client.models.generate_content.call_count, 5)  # 4 + 1 fallback

    def test_fallback_model_used_after_primary_exhausts_retries(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            ApiError(503)
        ] * ai_summarizer.MAX_RETRIES + [MagicMock(text="Fallback summary.")]
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"):
            summary = ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(summary, "Fallback summary.")
        models = [c.kwargs["model"] for c in client.models.generate_content.call_args_list]
        self.assertEqual(
            models,
            [ai_summarizer.MODEL] * ai_summarizer.MAX_RETRIES + [ai_summarizer.FALLBACK_MODEL],
        )


if __name__ == "__main__":
    unittest.main()
