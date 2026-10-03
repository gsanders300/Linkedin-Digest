import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google import genai

MODEL = "gemini-3.1-flash-lite"

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
        _client = genai.Client(api_key=api_key)
    return _client


# Bound request starts to nine per minute, leaving headroom under a 10 RPM quota.
# Multiple workers still help when individual requests take longer than six seconds.
MAX_WORKERS = 4
REQUESTS_PER_MINUTE = 9
MIN_REQUEST_INTERVAL = 60.0 / REQUESTS_PER_MINUTE

# Retry configuration for transient API failures.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds; doubled on each subsequent attempt

_rate_limit_lock = threading.Lock()
_next_request_at = 0.0


class _PermanentGeminiError(RuntimeError):
    """A Gemini response that should not be retried."""


def _wait_for_rate_slot() -> None:
    """Reserve the next quota-safe request start time across all workers."""
    global _next_request_at
    with _rate_limit_lock:
        now = time.monotonic()
        request_at = max(now, _next_request_at)
        _next_request_at = request_at + MIN_REQUEST_INTERVAL
    delay = request_at - now
    if delay > 0:
        time.sleep(delay)


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


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _PermanentGeminiError):
        return False
    status = _error_status_code(exc)
    if status is None:
        return True
    return status in {408, 409, 425, 429} or status >= 500


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


def summarize_post(post: dict) -> str:
    """Summarize a single post, incorporating author and engagement context.

    Retries up to MAX_RETRIES times with exponential backoff on transient API
    errors before raising RuntimeError so the caller can decide how to handle it.
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

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _wait_for_rate_slot()
            response = _get_client().models.generate_content(model=MODEL, contents=prompt)
            summary = response.text
            if not isinstance(summary, str) or not summary.strip():
                raise _PermanentGeminiError("Gemini returned an empty response.")
            return summary.strip()
        except Exception as exc:
            last_exc = exc
            if attempt >= MAX_RETRIES or not _is_retryable(exc):
                break

            exponential_delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay = max(exponential_delay, _retry_after(exc) or 0.0)
            delay += random.uniform(0.0, 0.5)
            print(
                f"Warning: Gemini attempt {attempt}/{MAX_RETRIES} failed for post "
                f"{post.get('id', '?')} — retrying in {delay:.1f}s. Error: {exc}"
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini API error for post {post.get('id', '?')} after {attempt} "
        f"attempt(s): {last_exc}"
    ) from last_exc


def summarize_posts(posts: list[dict]) -> tuple[list[dict], Optional[str]]:
    """Summarize all posts in parallel using a thread pool.

    Returns (posts_with_summaries, error_message).
    - On full success: (posts, None)
    - On partial/full failure: (posts, human-readable error string)

    Posts that fail summarization receive "Summary unavailable." so the email
    can still be sent with whatever content is available.
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

    # Map future -> post so results can be written back by reference.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_post = {executor.submit(summarize_post, post): post for post in posts}

        for future in as_completed(future_to_post):
            post = future_to_post[future]
            try:
                post["summary"] = future.result()
            except RuntimeError as exc:
                post["summary"] = "Summary unavailable."
                errors.append(str(exc))
                print(f"Warning: {exc}")

    if errors:
        # Deduplicate while preserving order.
        unique_errors = list(dict.fromkeys(errors))
        error_message = (
            f"{len(errors)} post(s) could not be summarized due to Gemini API errors. "
            f"First error: {unique_errors[0]}"
        )
        return posts, error_message

    return posts, None
