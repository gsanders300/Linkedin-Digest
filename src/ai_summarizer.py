import json
import math
import os
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google import genai
from google.genai import types

# Free-tier daily quotas are small (gemini-4-hydrogen's was 20 requests a day), so posts
# are summarized BATCH_SIZE to a request (see summarize_posts), not one request per post.
MODEL = "gemini-3.5-flash-lite"
# Tried after MODEL exhausts its retries, e.g. during a 503 "high demand" spike. Its
# 20-a-day free-tier limit covers a short spike, not a whole digest failing over.
FALLBACK_MODEL = "gemini-3.8-flash"

# The SDK sets no HTTP timeout by default, so one hung request could hold the job
# until GitHub kills it and no digest is sent. The SDK also sends this to Gemini as
# X-Server-Timeout, so a slow response comes back as 504 DEADLINE_EXCEEDED. A 10-post
# batch took 42-44s on gemini-4-hydrogen and hit 60s once under load.
REQUEST_TIMEOUT_MS = 120_000

# The Gemini client is created lazily rather than at import time, so a missing or
# invalid GEMINI_API_KEY becomes a caught, reportable error in the digest instead
# of crashing main.py on import before any email can be sent.
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Return a cached Gemini client, raising a clear error if the key is missing."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
    return _client


# Bound request starts to four per minute, under the 5 RPM free tier gemini-4-hydrogen had.
# Multiple workers still help when individual requests take longer than 15 seconds.
MAX_WORKERS = 4
REQUESTS_PER_MINUTE = 4
MIN_REQUEST_INTERVAL = 60.0 / REQUESTS_PER_MINUTE

# Posts summarized per request, so a 20-35 post day takes 2-4 requests. Posts a batch
# reply misses fall back to one request each. Same pattern as Youtube-Digest.
BATCH_SIZE = 10
# Per model. If both models fail, the batch's posts get no summary rather than one
# request each: during a Gemini outage that fan-out spent the whole daily quota.
# 4 tries back off 5s, 10s, 20s, enough to outlast a typical 503 spike. With 2 tries
# (one 5s wait), both models 503'd on 2026-11-21 and a whole batch went unsummarized.
BATCH_ATTEMPTS = 4
_JSON_CONFIG = types.GenerateContentConfig(response_mime_type="application/json")

# Retry configuration for transient API failures. Gemini 503 "high demand" spikes
# usually last tens of seconds, so back off 5s, 10s, 20s (~35s total). A post gets
# 5 tries in all, since each one spends daily quota.
MAX_RETRIES = 4
FALLBACK_ATTEMPTS = 1
RETRY_BASE_DELAY = 5.0  # seconds; doubled on each subsequent attempt

_rate_limit_lock = threading.Lock()
_next_request_at = 0.0

# Models whose daily quota ran out this run. They get no more requests, since each
# would fail the same way until the quota resets.
_quota_exhausted: set[str] = set()

# Every retry this run, as "model: why" -> count, for the digest's run status.
_retries: Counter[str] = Counter()
_retries_lock = threading.Lock()


def _note_retry(reason: str, count: int = 1) -> None:
    with _retries_lock:
        _retries[reason] += count


def retry_report() -> list[str]:
    """Return one line per kind of retry this run, e.g. "3x gemini-x: Gemini overloaded (503)"."""
    with _retries_lock:
        return [f"{count}x {reason}" for reason, count in _retries.items()]


class _PermanentGeminiError(RuntimeError):
    """A Gemini response that should not be retried."""


def _wait_for_rate_slot(deadline: float = math.inf) -> bool:
    """Reserve the next quota-safe request start time across all workers.

    Returns False, without reserving or waiting, if that start time is past deadline.
    """
    global _next_request_at
    with _rate_limit_lock:
        now = time.monotonic()
        request_at = max(now, _next_request_at)
        if request_at >= deadline:
            return False
        _next_request_at = request_at + MIN_REQUEST_INTERVAL
    delay = request_at - now
    if delay > 0:
        time.sleep(delay)
    return True


def _error_status_code(exc: Exception) -> int | None:
    """Extract an HTTP-style status code from common SDK exception shapes."""
    candidates = [getattr(exc, "code", None), getattr(exc, "status_code", None)]
    response = getattr(exc, "response", None)
    candidates.append(getattr(response, "status_code", None))
    for value in candidates:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _is_daily_quota(exc: Exception) -> bool:
    """True for a 429 from a per-day quota, which resets in hours, not seconds."""
    return _error_status_code(exc) == 429 and "PerDay" in str(exc)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _PermanentGeminiError) or _is_daily_quota(exc):
        return False
    status = _error_status_code(exc)
    if status is None:
        return True
    return status in {408, 409, 425, 429} or status >= 500


def _failure_reason(exc: Exception) -> str:
    """Name the cause of a failed Gemini call in a short phrase for the email."""
    if isinstance(exc, _PermanentGeminiError):
        return str(exc).rstrip(".")
    status = _error_status_code(exc)
    if _is_daily_quota(exc):
        return "free-tier daily request quota used up (429)"
    if status == 429:
        return "free-tier per-minute rate limit hit (429)"
    if status == 503:
        return "Gemini overloaded (503)"
    if status == 504:
        return "Gemini timed out (504)"
    if status is not None:
        return f"Gemini returned HTTP {status}"
    return f"{type(exc).__name__} (no HTTP status; see job log)"


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


SUMMARY_PROMPT_TEMPLATE = """You are analyzing a LinkedIn post. Provide EXACTLY 2 concise, professional sentences that summarize the post.
Requirements:
- Sentence 1: State the main topic or message
- Sentence 2: Highlight the key insight, conclusion, or takeaway

Keep it professional, informative, and business-focused. No more, no less than 2 sentences.

Author: {author_name}
Engagement: {likes} likes · {comments} comments · {reposts} reposts

LinkedIn Post:
{post_text}

2-Sentence Summary:"""


def summarize_post(post: dict, deadline: float = math.inf) -> str:
    """Summarize a single post, incorporating author and engagement context.

    Tries MODEL up to MAX_RETRIES times with exponential backoff on transient API
    errors, then FALLBACK_MODEL up to FALLBACK_ATTEMPTS times. Raises RuntimeError if both fail
    so the caller can decide how to handle it.
    No request starts after deadline, a time.monotonic() value.
    """
    text = post.get("text", "")
    if not text:
        return "No content available."

    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        author_name=post.get("profile_name", "Unknown"),
        likes=post.get("likes", 0),
        comments=post.get("comments", 0),
        reposts=post.get("reposts", 0),
        post_text=text[:5000],
    )

    errors: list[str] = []
    for model, attempts in ((MODEL, MAX_RETRIES), (FALLBACK_MODEL, FALLBACK_ATTEMPTS)):
        try:
            summary = _generate_with_retries(
                model, prompt, f"post {post.get('id', '?')}", deadline, attempts=attempts
            )
            post["model"] = model
            return summary
        except RuntimeError as exc:
            errors.append(str(exc))
            if model != FALLBACK_MODEL:
                print(f"Warning: {exc} Trying fallback model {FALLBACK_MODEL}.")
    raise RuntimeError(" | ".join(errors))


BATCH_PROMPT_TEMPLATE = """You are analyzing LinkedIn posts. For EACH post below, write EXACTLY 2 concise, professional sentences that summarize it.
Requirements:
- Sentence 1: State the main topic or message
- Sentence 2: Highlight the key insight, conclusion, or takeaway

Keep it professional, informative, and business-focused. No more, no less than 2 sentences per post.

Respond with ONLY a JSON object mapping each post number to its summary string, for example {{"1": "Summary.", "2": "Summary."}}.

{posts_block}"""

BATCH_POST_TEMPLATE = """=== Post {number} ===
Author: {author_name}
Engagement: {likes} likes · {comments} comments · {reposts} reposts

{post_text}"""


def _summarize_batch(posts: list[dict], deadline: float) -> dict[int, str]:
    """Summarize posts in one request; return {index in posts: summary} for each usable answer.

    Posts are numbered 1..n in the prompt rather than by their 19-digit IDs, which a
    model can miscopy. Returns {} if the JSON is unusable, so the caller summarizes
    those posts one at a time. Raises RuntimeError if both models fail.
    """
    posts_block = "\n\n".join(
        BATCH_POST_TEMPLATE.format(
            number=number,
            author_name=post.get("profile_name", "Unknown"),
            likes=post.get("likes", 0),
            comments=post.get("comments", 0),
            reposts=post.get("reposts", 0),
            post_text=post["text"][:5000],
        )
        for number, post in enumerate(posts, 1)
    )
    prompt = BATCH_PROMPT_TEMPLATE.format(posts_block=posts_block)
    label = f"batch of {len(posts)} posts"

    errors: list[str] = []
    for model in (MODEL, FALLBACK_MODEL):
        try:
            text = _generate_with_retries(
                model, prompt, label, deadline, attempts=BATCH_ATTEMPTS, config=_JSON_CONFIG
            )
            break
        except RuntimeError as exc:
            errors.append(str(exc))
            print(f"Warning: {exc}")
    else:
        raise RuntimeError(" | ".join(errors))

    try:
        answers = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"Warning: {label} returned invalid JSON ({exc}); summarizing them one at a time.")
        _note_retry(f"{model}: batch reply was invalid JSON; post re-asked alone", len(posts))
        return {}
    if not isinstance(answers, dict):
        print(f"Warning: {label} returned JSON that isn't an object; summarizing them one at a time.")
        _note_retry(f"{model}: batch reply wasn't a JSON object; post re-asked alone", len(posts))
        return {}
    summaries = {
        int(key) - 1: summary.strip()
        for key, summary in answers.items()
        if key.isdecimal()
        and 1 <= int(key) <= len(posts)
        and isinstance(summary, str)
        and summary.strip()
    }
    for index in summaries:
        posts[index]["model"] = model
    if len(summaries) < len(posts):
        _note_retry(
            f"{model}: batch reply skipped a post; post re-asked alone",
            len(posts) - len(summaries),
        )
    return summaries


def _generate_with_retries(
    model: str,
    prompt: str,
    label: str,
    deadline: float,
    attempts: int = MAX_RETRIES,
    config: types.GenerateContentConfig | None = None,
) -> str:
    """Call one Gemini model with retries; raise RuntimeError when it gives up."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        if model in _quota_exhausted:
            raise RuntimeError(f"Skipped {label}: {model}'s daily quota is used up.")
        if not _wait_for_rate_slot(deadline):
            raise RuntimeError(f"Skipped {label}: the summarization time limit ran out.")
        if attempt == 1 and model == FALLBACK_MODEL:
            # Both callers try FALLBACK_MODEL only after MODEL failed.
            _note_retry(f"{model}: took over after {MODEL} failed")
        try:
            response = _get_client().models.generate_content(
                model=model, contents=prompt, config=config
            )
            summary = response.text
            if not isinstance(summary, str) or not summary.strip():
                raise _PermanentGeminiError("Gemini returned an empty response.")
            return summary.strip()
        except Exception as exc:
            last_exc = exc
            if _is_daily_quota(exc):
                _quota_exhausted.add(model)
            if attempt >= attempts or not _is_retryable(exc):
                break

            exponential_delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay = max(exponential_delay, _retry_after(exc) or 0.0)
            delay += random.uniform(0.0, 0.5)
            if time.monotonic() + delay >= deadline:
                break
            print(
                f"Warning: {model} attempt {attempt}/{attempts} failed for {label} "
                f"— retrying in {delay:.1f}s. Error: {exc}"
            )
            _note_retry(f"{model}: {_failure_reason(exc)}")
            time.sleep(delay)

    hint = ""
    if _is_daily_quota(last_exc):
        hint = (
            f" Daily quota used up: {model}'s free tier allows too few requests a day. "
            "Set MODEL in src/ai_summarizer.py to a model with a higher daily limit, "
            "or enable billing in Google AI Studio."
        )
    elif _error_status_code(last_exc) == 429:
        hint = (
            f" Rate limit hit: {model} allows fewer than REQUESTS_PER_MINUTE "
            f"({REQUESTS_PER_MINUTE}) requests a minute, or its daily quota is used up. "
            "Lower REQUESTS_PER_MINUTE in src/ai_summarizer.py or check your quota "
            "in Google AI Studio."
        )
    raise RuntimeError(
        f"{model} gave up on {label} after {attempt} attempt(s): "
        # Cap the raw error so a long quota body can't push the next model's cause out.
        f"{_failure_reason(last_exc)}.{hint} Details: {str(last_exc)[:300]}"
    ) from last_exc


def summarize_posts(
    posts: list[dict], deadline: float = math.inf
) -> tuple[list[dict], Optional[str]]:
    """Summarize posts BATCH_SIZE to a request, in parallel using a thread pool.

    Posts a batch reply misses are summarized one request each with summarize_post().
    Posts in a batch that fails on both models are not retried one at a time.

    Returns (posts_with_summaries, error_message).
    - On full success: (posts, None)
    - On partial/full failure: (posts, human-readable error string)

    Posts that fail summarization, or are not reached by deadline (a time.monotonic()
    value), receive "Summary unavailable." so the email can still be sent before the
    workflow times out.
    """
    if not posts:
        return posts, None

    # Validate the client once up front so a missing or invalid key fails fast and
    # cleanly, instead of driving every post through the full retry/backoff loop.
    try:
        _get_client()
    except RuntimeError as exc:
        for post in posts:
            post["summary"] = "Summary unavailable."
        return posts, str(exc)

    errors: list[str] = []

    # Posts with no text skip the batch; summarize_post() answers them without a request.
    with_text = [post for post in posts if post.get("text")]
    remaining = [post for post in posts if not post.get("text")]
    batches = [with_text[i : i + BATCH_SIZE] for i in range(0, len(with_text), BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        batch_futures = [executor.submit(_summarize_batch, batch, deadline) for batch in batches]
        for batch, batch_future in zip(batches, batch_futures):
            try:
                summaries = batch_future.result()
            except RuntimeError as exc:
                for post in batch:
                    post["summary"] = "Summary unavailable."
                errors.extend([str(exc)] * len(batch))
                print(f"Warning: {exc}")
                continue
            for index, post in enumerate(batch):
                if index in summaries:
                    post["summary"] = summaries[index]
                else:
                    remaining.append(post)

        # Map future -> post so results can be written back by reference.
        future_to_post = {
            executor.submit(summarize_post, post, deadline): post for post in remaining
        }

        for future in as_completed(future_to_post):
            post = future_to_post[future]
            try:
                post["summary"] = future.result()
            except RuntimeError as exc:
                post["summary"] = "Summary unavailable."
                errors.append(str(exc))
                print(f"Warning: {exc}")

    if errors:
        causes = "\n".join(
            f"- {count} post(s): {error}" for error, count in Counter(errors).items()
        )
        error_message = f"{len(errors)} post(s) could not be summarized.\nCauses:\n{causes}"
        return posts, error_message

    return posts, None
