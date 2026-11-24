import json
import os
import time
import unittest
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic

import ai_summarizer
import email_sender
from ai_summarizer import _is_retryable

DAILY_QUOTA = "429 RESOURCE_EXHAUSTED. quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"


class ApiError(Exception):
    """A Gemini-style error with an HTTP status in .code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.code = code


def _claude_error(status: int = 529, message: str = "overloaded") -> anthropic.APIStatusError:
    response = MagicMock(status_code=status, headers={})
    return anthropic.APIStatusError(message, response=response, body=None)


def _claude_reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    """A Messages API response: a thinking block, then the answer."""
    content = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _is_batch(request: dict) -> bool:
    return "=== Post " in request["messages"][0]["content"]


def _patch_models(claude: MagicMock, gemini: MagicMock) -> ExitStack:
    """Patch in mock Claude and Gemini clients and an open rate limiter."""
    stack = ExitStack()
    stack.enter_context(patch.object(ai_summarizer, "_get_claude_client", return_value=claude))
    stack.enter_context(patch.object(ai_summarizer, "_get_client", return_value=gemini))
    stack.enter_context(patch.object(ai_summarizer, "_wait_for_rate_slot", return_value=True))
    return stack


class RetryTests(unittest.TestCase):
    def test_rate_limit_and_server_errors_are_retryable(self) -> None:
        self.assertTrue(_is_retryable(ApiError(429)))
        self.assertTrue(_is_retryable(ApiError(503)))
        self.assertTrue(_is_retryable(_claude_error(529)))

    def test_authentication_and_bad_request_errors_are_not_retryable(self) -> None:
        self.assertFalse(_is_retryable(ApiError(400)))
        self.assertFalse(_is_retryable(ApiError(401)))
        self.assertFalse(_is_retryable(ApiError(403)))
        self.assertFalse(_is_retryable(_claude_error(400)))
        self.assertFalse(_is_retryable(_claude_error(401)))


class ClientTests(unittest.TestCase):
    def test_client_sets_request_timeout(self) -> None:
        with patch.object(ai_summarizer, "_client", None), patch.dict(
            os.environ, {"GEMINI_API_KEY": "key"}
        ), patch.object(ai_summarizer.genai, "Client") as client_class:
            ai_summarizer._get_client()

        options = client_class.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, ai_summarizer.REQUEST_TIMEOUT_MS)

    def test_claude_client_sets_timeout_and_no_sdk_retries(self) -> None:
        with patch.object(ai_summarizer, "_claude_client", None), patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "key"}
        ), patch.object(ai_summarizer.anthropic, "Anthropic") as client_class:
            ai_summarizer._get_claude_client()

        client_class.assert_called_once_with(api_key="key", timeout=120, max_retries=0)

    def test_missing_anthropic_key_fails_fast(self) -> None:
        with patch.object(ai_summarizer, "_claude_client", None), patch.dict(
            os.environ, {"GEMINI_API_KEY": "key"}
        ), patch.object(ai_summarizer, "_get_client") as gemini:
            os.environ.pop("ANTHROPIC_API_KEY", None)  # patch.dict restores it
            result, error = ai_summarizer.summarize_posts([{"id": "1", "text": "Hello"}])

        gemini.return_value.models.generate_content.assert_not_called()
        self.assertEqual(result[0]["summary"], "Summary unavailable.")
        self.assertIn("ANTHROPIC_API_KEY", error or "")


class ClaudeTests(unittest.TestCase):
    def test_claude_answers_at_high_effort(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.return_value = _claude_reply("Summary.")
        post = {"id": "1", "text": "Hello"}
        with _patch_models(claude, gemini):
            summary = ai_summarizer.summarize_post(post)

        self.assertEqual(summary, "Summary.")
        self.assertEqual(post["model"], ai_summarizer.MODEL)
        request = claude.messages.create.call_args.kwargs
        self.assertEqual(request["model"], "claude-haiku-5-5")
        self.assertEqual(request["output_config"], {"effort": "high"})
        self.assertEqual(request["max_tokens"], ai_summarizer.CLAUDE_MAX_TOKENS)
        self.assertNotIn("temperature", request)  # Haiku 5.5 rejects sampling parameters
        gemini.models.generate_content.assert_not_called()

    def test_refusal_or_cut_off_reply_goes_to_gemini_without_retry(self) -> None:
        for stop_reason, cause in (
            ("refusal", "Claude declined to summarize it"),
            ("max_tokens", "Claude's reply hit max_tokens"),
        ):
            with self.subTest(stop_reason=stop_reason):
                claude, gemini = MagicMock(), MagicMock()
                claude.messages.create.return_value = _claude_reply("", stop_reason)
                gemini.models.generate_content.return_value = MagicMock(text="Fallback summary.")
                post = {"id": "1", "text": "Hello"}
                with _patch_models(claude, gemini), patch.object(
                    ai_summarizer.time, "sleep"
                ) as sleep, patch.object(ai_summarizer, "_retries", Counter()), patch(
                    "builtins.print"
                ) as log:
                    summary = ai_summarizer.summarize_post(post)
                    retries = ai_summarizer.retry_report()

                self.assertEqual(summary, "Fallback summary.")
                self.assertEqual(post["model"], ai_summarizer.FALLBACK_MODEL)
                self.assertEqual(claude.messages.create.call_count, 1)
                sleep.assert_not_called()
                self.assertIn(cause, str(log.call_args_list))
                self.assertEqual(
                    retries,
                    [f"1x {ai_summarizer.FALLBACK_MODEL}: took over after {ai_summarizer.MODEL} failed"],
                )


class SummarizerTests(unittest.TestCase):
    def test_expired_deadline_skips_requests_but_returns_posts(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        posts = [{"id": "1", "text": "Hello"}, {"id": "2", "text": "World"}]
        with patch.object(ai_summarizer, "_get_claude_client", return_value=claude), patch.object(
            ai_summarizer, "_get_client", return_value=gemini
        ), patch.object(ai_summarizer, "_retries", Counter()):
            result, error = ai_summarizer.summarize_posts(
                posts, deadline=time.monotonic() - 1
            )
            # No request was sent, so the fallback never "took over".
            self.assertEqual(ai_summarizer.retry_report(), [])

        claude.messages.create.assert_not_called()
        gemini.models.generate_content.assert_not_called()
        self.assertEqual(
            [post["summary"] for post in result], ["Summary unavailable."] * 2
        )
        self.assertIn("time limit ran out", error or "")

    def test_retry_is_skipped_when_backoff_would_pass_deadline(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        with patch.object(ai_summarizer, "_get_claude_client", return_value=claude), patch.object(
            ai_summarizer, "_get_client", return_value=gemini
        ), patch.object(ai_summarizer, "_next_request_at", 0.0), patch.object(
            ai_summarizer.time, "sleep"
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 1 attempt"):
                ai_summarizer.summarize_post(
                    {"id": "1", "text": "Hello"}, deadline=time.monotonic() + 0.5
                )

        sleep.assert_not_called()
        gemini.models.generate_content.assert_not_called()

    def test_overload_spike_recovers_within_retry_budget(self) -> None:
        claude = MagicMock()
        claude.messages.create.side_effect = [_claude_error(529)] * 3 + [
            _claude_reply("Summary.")
        ]
        with _patch_models(claude, MagicMock()), patch.object(
            ai_summarizer.time, "sleep"
        ) as sleep:
            summary = ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(summary, "Summary.")
        backoffs = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(backoffs), 3)
        self.assertGreaterEqual(backoffs[-1], 20.0)

    def test_batch_overload_spike_recovers_on_primary_model(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = [_claude_error(529)] * 3 + [
            _claude_reply('{"1": "Summary."}')
        ]
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep") as sleep:
            result, error = ai_summarizer.summarize_posts([{"id": "1", "text": "Hello"}])

        self.assertIsNone(error)
        self.assertEqual(result[0]["summary"], "Summary.")
        self.assertEqual(result[0]["model"], ai_summarizer.MODEL)
        self.assertEqual(claude.messages.create.call_count, 4)
        gemini.models.generate_content.assert_not_called()
        self.assertGreaterEqual(sleep.call_args_list[-1].args[0], 20.0)

    def test_rate_limit_failure_explains_how_to_fix(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(429)
        gemini.models.generate_content.side_effect = ApiError(429)
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        message = str(ctx.exception)
        self.assertIn("Claude rate limit hit (429)", message)
        # The hint is Gemini advice, so Claude's 429 doesn't get it.
        self.assertEqual(message.count("Lower REQUESTS_PER_MINUTE"), 1)

    def test_daily_quota_429_skips_retries_and_explains(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(400, "bad request")
        error = ApiError(429)
        error.args = (DAILY_QUOTA,)
        gemini.models.generate_content.side_effect = error
        with _patch_models(claude, gemini), patch.object(
            ai_summarizer.time, "sleep"
        ) as sleep, patch.object(ai_summarizer, "_quota_exhausted", set()):
            with self.assertRaisesRegex(RuntimeError, "Daily quota used up"):
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(claude.messages.create.call_count, 1)  # 400 isn't retried
        self.assertEqual(gemini.models.generate_content.call_count, 1)
        sleep.assert_not_called()

    def test_model_with_used_up_daily_quota_gets_no_more_requests(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(400, "bad request")
        error = ApiError(429)
        error.args = (DAILY_QUOTA,)
        gemini.models.generate_content.side_effect = error
        with _patch_models(claude, gemini), patch.object(ai_summarizer, "_quota_exhausted", set()):
            for post_id in ("1", "2", "3"):
                with self.assertRaises(RuntimeError):
                    ai_summarizer.summarize_post({"id": post_id, "text": "Hello"})

        self.assertEqual(claude.messages.create.call_count, 3)
        self.assertEqual(gemini.models.generate_content.call_count, 1)

    def test_failed_batch_is_not_retried_one_post_at_a_time(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        gemini.models.generate_content.side_effect = ApiError(503)
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"):
            result, error = ai_summarizer.summarize_posts(posts)

        # BATCH_ATTEMPTS per model, and no single-post requests.
        self.assertEqual(claude.messages.create.call_count, ai_summarizer.BATCH_ATTEMPTS)
        self.assertEqual(
            gemini.models.generate_content.call_count, ai_summarizer.BATCH_ATTEMPTS
        )
        self.assertEqual(
            [post["summary"] for post in result], ["Summary unavailable."] * 2
        )
        self.assertIn("2 post(s) could not be summarized", error or "")

    def test_posts_are_batched_and_unicode_survives(self) -> None:
        summary = "Isn’t “this” great 🚀."
        posts = [{"id": str(i), "text": f"Post {i}"} for i in range(12)]

        def respond(**request):
            count = request["messages"][0]["content"].count("=== Post ")
            return _claude_reply(json.dumps({str(n): summary for n in range(1, count + 1)}))

        claude = MagicMock()
        claude.messages.create.side_effect = respond
        with _patch_models(claude, MagicMock()):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual(claude.messages.create.call_count, 2)  # 10 + 2
        self.assertEqual([post["summary"] for post in result], [summary] * 12)

    def test_post_missing_from_batch_is_summarized_alone(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]

        def respond(**request):
            if _is_batch(request):
                return _claude_reply('{"1": "Batch summary.", "9": "Unknown post."}')
            return _claude_reply("Single summary.")

        claude = MagicMock()
        claude.messages.create.side_effect = respond
        with _patch_models(claude, MagicMock()):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual(
            [post["summary"] for post in result], ["Batch summary.", "Single summary."]
        )
        self.assertEqual(claude.messages.create.call_count, 2)

    def test_invalid_batch_json_falls_back_to_one_request_per_post(self) -> None:
        posts = [{"id": "a", "text": "First"}, {"id": "b", "text": "Second"}]

        def respond(**request):
            if _is_batch(request):
                return _claude_reply('{"1": "cut off')
            return _claude_reply("Single summary.")

        claude = MagicMock()
        claude.messages.create.side_effect = respond
        with _patch_models(claude, MagicMock()):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIsNone(error)
        self.assertEqual([post["summary"] for post in result], ["Single summary."] * 2)
        self.assertEqual(claude.messages.create.call_count, 3)  # 1 batch + 2 single

    def test_fallback_answer_reaches_run_status_with_retries_and_model(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        gemini.models.generate_content.return_value = MagicMock(text='{"1": "Summary."}')
        posts = [{"id": "1", "text": "Hello"}]
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            result, error = ai_summarizer.summarize_posts(posts)
            retries = ai_summarizer.retry_report()

        self.assertIsNone(error)
        self.assertEqual(result[0]["model"], ai_summarizer.FALLBACK_MODEL)
        self.assertIn(f"3x {ai_summarizer.MODEL}: Claude overloaded (529)", retries)
        self.assertIn(
            f"1x {ai_summarizer.FALLBACK_MODEL}: took over after {ai_summarizer.MODEL} failed",
            retries,
        )

        rows = dict(email_sender._run_status(result, None, None, retries))
        self.assertEqual(rows["Problems"], "None")
        self.assertIn("Claude overloaded (529)", rows["Retries"])
        self.assertEqual(
            rows["Models that answered"], f"{ai_summarizer.FALLBACK_MODEL}: 1 post(s)"
        )

    def test_failed_batch_names_cause_in_error_and_subject(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        gemini.models.generate_content.side_effect = ApiError(503)
        posts = [{"id": "1", "text": "Hello"}]
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            result, error = ai_summarizer.summarize_posts(posts)

        self.assertIn("1 post(s) could not be summarized.\nCauses:", error or "")
        self.assertIn("Claude overloaded (529)", error or "")
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
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(400, "bad request " + "x" * 2000)
        gemini.models.generate_content.side_effect = ApiError(503)
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"), patch.object(
            ai_summarizer, "_retries", Counter()
        ):
            _result, error = ai_summarizer.summarize_posts([{"id": "1", "text": "Hello"}])

        self.assertIn("Claude returned HTTP 400", error or "")
        self.assertIn(f"{ai_summarizer.FALLBACK_MODEL} gave up", error or "")
        self.assertIn("Gemini overloaded (503)", error or "")

    def test_server_error_failure_has_no_rate_limit_hint(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        gemini.models.generate_content.side_effect = ApiError(503)
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertNotIn("REQUESTS_PER_MINUTE", str(ctx.exception))
        self.assertEqual(claude.messages.create.call_count, ai_summarizer.MAX_RETRIES)
        self.assertEqual(gemini.models.generate_content.call_count, 1)

    def test_fallback_model_used_after_primary_exhausts_retries(self) -> None:
        claude, gemini = MagicMock(), MagicMock()
        claude.messages.create.side_effect = _claude_error(529)
        gemini.models.generate_content.return_value = MagicMock(text="Fallback summary.")
        with _patch_models(claude, gemini), patch.object(ai_summarizer.time, "sleep"):
            summary = ai_summarizer.summarize_post({"id": "1", "text": "Hello"})

        self.assertEqual(summary, "Fallback summary.")
        self.assertEqual(claude.messages.create.call_count, ai_summarizer.MAX_RETRIES)
        models = [c.kwargs["model"] for c in gemini.models.generate_content.call_args_list]
        self.assertEqual(models, [ai_summarizer.FALLBACK_MODEL])


if __name__ == "__main__":
    unittest.main()
