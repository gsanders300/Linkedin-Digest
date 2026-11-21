import json
import os
import time
import unittest
from collections import Counter
from unittest.mock import MagicMock, patch

import ai_summarizer
import email_sender
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
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            result, error = ai_summarizer.summarize_posts(
                posts, deadline=time.monotonic() - 1
            )
            # No request was sent, so the fallback never "took over".
            self.assertEqual(ai_summarizer.retry_report(), [])

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

    def test_batch_503_spike_recovers_on_primary_model(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [ApiError(503)] * 3 + [
            MagicMock(text='{"1": "Summary."}')
        ]
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep") as sleep:
            result, error = ai_summarizer.summarize_posts([{"id": "1", "text": "Hello"}])

        self.assertIsNone(error)
        self.assertEqual(result[0]["summary"], "Summary.")
        models = [c.kwargs["model"] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models, [ai_summarizer.MODEL] * 4)
        self.assertGreaterEqual(sleep.call_args_list[-1].args[0], 20.0)

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
        ), patch.object(ai_summarizer.time, "sleep") as sleep, patch.object(
            ai_summarizer, "_quota_exhausted", set()
        ):
            with self.assertRaisesRegex(RuntimeError, "Daily quota used up"):
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(client.models.generate_content.call_count, 2)  # 1 per model
        sleep.assert_not_called()

    def test_model_with_used_up_daily_quota_gets_no_more_requests(self) -> None:
        error = ApiError(429)
        error.args = ("429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",)

        def respond(model, contents, config):
            if model == ai_summarizer.MODEL:
                raise error
            return MagicMock(text="Fallback summary.")

        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer, "_quota_exhausted", set()):
            for post_id in ("1", "2", "3"):
                ai_summarizer.summarize_post({"id": post_id, "text": "Hello"})

        models = [c.kwargs["model"] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models.count(ai_summarizer.MODEL), 1)
        self.assertEqual(models.count(ai_summarizer.FALLBACK_MODEL), 3)

    def test_failed_batch_is_not_retried_one_post_at_a_time(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]
        client = MagicMock()
        client.models.generate_content.side_effect = ApiError(503)
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"):
            result, error = ai_summarizer.summarize_posts(posts)

        # BATCH_ATTEMPTS per model, and no single-post requests.
        self.assertEqual(
            client.models.generate_content.call_count, 2 * ai_summarizer.BATCH_ATTEMPTS
        )
        self.assertEqual(
            [post["summary"] for post in result], ["Summary unavailable."] * 2
        )
        self.assertIn("2 post(s) could not be summarized", error or "")

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

    def test_fallback_answer_reaches_run_status_with_retries_and_model(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = [
            ApiError(503)
        ] * ai_summarizer.BATCH_ATTEMPTS + [MagicMock(text='{"1": "Summary."}')]
        posts = [{"id": "1", "text": "Hello"}]
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            result, error = ai_summarizer.summarize_posts(posts)
            retries = ai_summarizer.retry_report()

        self.assertIsNone(error)
        self.assertEqual(result[0]["model"], ai_summarizer.FALLBACK_MODEL)
        self.assertIn(f"3x {ai_summarizer.MODEL}: Gemini overloaded (503)", retries)
        self.assertIn(
            f"1x {ai_summarizer.FALLBACK_MODEL}: took over after {ai_summarizer.MODEL} failed",
            retries,
        )

        rows = dict(email_sender._run_status(result, None, None, retries))
        self.assertEqual(rows["Problems"], "None")
        self.assertIn("Gemini overloaded (503)", rows["Retries"])
        self.assertEqual(
            rows["Models that answered"], f"{ai_summarizer.FALLBACK_MODEL}: 1 post(s)"
        )

    def test_failed_batch_names_cause_in_error_and_subject(self) -> None:
        client = MagicMock()
        client.models.generate_content.side_effect = ApiError(503)
        posts = [{"id": "1", "text": "Hello"}]
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIn("1 post(s) could not be summarized.\nCauses:", error or "")
        self.assertIn("Gemini overloaded (503)", error or "")
        rows = dict(email_sender._run_status(result, None, error, []))
        self.assertTrue(rows["Problems"].startswith("1 post(s) could not be summarized."))
        self.assertEqual(rows["Models that answered"], "none (no summary): 1 post(s)")
        html = email_sender.build_html_digest(result, gemini_error=error, usage=None)
        self.assertIn("Run status", html)
        self.assertEqual(
            email_sender._subject_line(result, None, True), "LinkedIn Digest - 1 New Post [problems]"
        )

    def test_long_primary_error_does_not_hide_fallback_cause(self) -> None:
        quota = ApiError(429)
        quota.args = ("429 RESOURCE_EXHAUSTED PerDay " + "x" * 2000,)

        def respond(model, contents, config):
            raise quota if model == ai_summarizer.MODEL else ApiError(503)

        client = MagicMock()
        client.models.generate_content.side_effect = respond
        with patch.object(ai_summarizer, "_get_client", return_value=client), patch.object(
            ai_summarizer, "_wait_for_rate_slot", return_value=True
        ), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ), patch.object(ai_summarizer, "_quota_exhausted", set()):
            _result, error = ai_summarizer.summarize_posts([{"id": "1", "text": "Hello"}])

        self.assertIn("free-tier daily request quota used up (429)", error or "")
        self.assertIn(f"{ai_summarizer.FALLBACK_MODEL} gave up", error or "")
        self.assertIn("Gemini overloaded (503)", error or "")

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
