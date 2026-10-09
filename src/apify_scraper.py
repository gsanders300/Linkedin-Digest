import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from apify_client import ApifyClient

from utils import LOOKBACK_HOURS, parse_post_date

# harvestapi/linkedin-profile-posts — no cookies required, flat output schema.
ACTOR_ID = "harvestapi/linkedin-profile-posts"

# Apify REST API base URL (v2).
APIFY_API_BASE = "https://api.apify.com/v2"


def _safe_int(value: object) -> int:
    """Convert an Actor counter to int without failing the whole batch."""
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalise_url(value: object) -> str:
    """Return a URL without query parameters, fragments, or a trailing slash."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _normalise_post_item(item: dict) -> dict | None:
    """Convert one Actor dataset item to the internal post schema."""
    if item.get("type") not in ("post", None, ""):
        return None

    post_url = _normalise_url(item.get("linkedinUrl"))
    raw_id = item.get("id")
    post_id = str(raw_id).strip() if raw_id is not None else ""
    if not post_id and post_url:
        post_id = urlsplit(post_url).path.rstrip("/").rsplit("/", 1)[-1]
    if not post_id:
        return None

    author = item.get("author")
    if not isinstance(author, dict):
        author = {}
    raw_name = author.get("name")
    profile_name = raw_name.strip() if isinstance(raw_name, str) else ""
    profile_name = profile_name or "LinkedIn User"
    author_profile_url = _normalise_url(author.get("linkedinUrl"))

    posted_at = item.get("postedAt")
    raw_date = posted_at.get("date") if isinstance(posted_at, dict) else posted_at
    published_date = raw_date.strip() if isinstance(raw_date, str) else ""

    engagement = item.get("engagement")
    if not isinstance(engagement, dict):
        engagement = {}

    content = item.get("content")
    if not isinstance(content, str):
        content = ""

    return {
        "id": post_id,
        "profile_name": profile_name,
        "profile_url": author_profile_url,
        "post_url": f"{post_url}/" if post_url else "",
        "text": content,
        "published_date": published_date,
        "likes": _safe_int(engagement.get("likes")),
        "comments": _safe_int(engagement.get("comments")),
        "reposts": _safe_int(engagement.get("shares")),
    }


def _get_client() -> ApifyClient:
    """Return an ApifyClient, raising clearly if the token is missing."""
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError(
            "APIFY_API_TOKEN environment variable is not set. "
            "Cannot initialise the Apify client."
        )
    return ApifyClient(token)


def _fetch_plan_limit(headers: dict) -> float:
    """Return the monthly credit limit in USD from the user profile endpoint."""
    resp = requests.get(f"{APIFY_API_BASE}/users/me", headers=headers, timeout=10)
    resp.raise_for_status()
    plan = resp.json().get("data", {}).get("plan", {})
    return float(plan.get("monthlyUsageCreditsUsd", 0))


def _fetch_used_credits(headers: dict) -> float:
    """Return credits consumed so far this billing cycle in USD."""
    resp = requests.get(
        f"{APIFY_API_BASE}/users/me/usage/monthly", headers=headers, timeout=10
    )
    resp.raise_for_status()
    usage_data = resp.json().get("data", {})
    return float(usage_data.get("totalUsageCreditsUsdAfterVolumeDiscount", 0))


def get_apify_usage_stats() -> Optional[dict]:
    """Fetch the current monthly credit usage from the Apify API.

    Returns a dict with keys:
        used_usd  (float) - credits consumed so far this billing cycle
        limit_usd (float) - total credits included in the current plan
        percent   (float) - used_usd / limit_usd * 100, or 0.0 if limit is zero

    Returns None if the API calls fail for any reason.
    """
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}

    try:
        # Fetch plan limit and usage in parallel — they are independent requests.
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_limit = executor.submit(_fetch_plan_limit, headers)
            future_used = executor.submit(_fetch_used_credits, headers)
            limit_usd: float = future_limit.result()
            used_usd: float = future_used.result()

        percent = (used_usd / limit_usd * 100) if limit_usd > 0 else 0.0
        return {"used_usd": used_usd, "limit_usd": limit_usd, "percent": percent}

    except Exception as exc:
        print(f"Warning: could not fetch Apify usage stats: {exc}")
        return None


def scrape_profiles(profile_urls: list[str]) -> tuple[list, Optional[str]]:
    """Scrape LinkedIn posts for all given profile URLs in a single Apify batch run.

    Returns a tuple of (posts, error_message).
    - On success: (list_of_posts, None)
    - On failure: ([], descriptive_error_string)

    The error string distinguishes credit-exhaustion failures from other errors.
    """
    all_posts: list[dict] = []
    print(f"Scraping {len(profile_urls)} profiles...")

    try:
        client = _get_client()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        run_input = {
            "targetUrls": profile_urls,
            "maxPosts": 2,          # Maximum 2 posts per profile (reduces billable results).
            "includeQuotePosts": False,
            "includeReposts": False,
            "scrapeComments": False,
            "scrapeReactions": False,
            "maxComments": 0,
            "maxReactions": 0,
            # Actor-side filter; utils.filter_new_posts is a safety net.
            "postedLimitDate": cutoff.isoformat(timespec="milliseconds"),
        }

        # Stop an Actor run after 10 minutes so it cannot hold the CI job open.
        run = client.actor(ACTOR_ID).call(
            run_input=run_input,
            run_timeout=timedelta(seconds=600),
        )

        if run is None:
            return [], "Apify actor run returned no result (it may have timed out)."

        # Confirm the run actually succeeded. A FAILED/ABORTED/TIMED-OUT run leaves
        # an empty dataset that would otherwise be indistinguishable from a genuine
        # quiet day. If the status field is unreadable, proceed rather than raise a
        # false alarm on an otherwise-working run.
        status = getattr(run, "status", None)
        if status is not None and status != "SUCCEEDED":
            return [], (
                f"Apify actor run did not succeed (status: {status}). "
                "No posts were retrieved."
            )

        dataset_items = client.dataset(run.default_dataset_id).list_items().items

        print(f"Retrieved {len(dataset_items)} dataset items from run.")

        malformed_items = 0
        for index, item in enumerate(dataset_items):
            if not isinstance(item, dict):
                malformed_items += 1
                print(f"Warning: skipping non-dictionary dataset item at index {index}.")
                continue

            try:
                post = _normalise_post_item(item)
            except Exception as exc:
                malformed_items += 1
                print(f"Warning: skipping malformed dataset item at index {index}: {exc}")
                continue

            if post is not None:
                all_posts.append(post)

        if dataset_items and not all_posts and malformed_items:
            return [], (
                f"Apify returned {len(dataset_items)} dataset item(s), but none could "
                "be processed. The Actor output schema may have changed."
            )

        # Posts without a readable date are dropped by the lookback filter. If none can
        # be read, the date field has likely changed, which would otherwise look like
        # a quiet day.
        undated = sum(1 for p in all_posts if parse_post_date(p["published_date"]) is None)
        if undated:
            print(f"Warning: {undated} post(s) have no readable publish date and will be skipped.")
        if all_posts and undated == len(all_posts):
            return [], (
                f"Apify returned {len(all_posts)} post(s), but none had a readable "
                "publish date. The Actor output schema may have changed."
            )

        print(f"Successfully processed {len(all_posts)} posts total.")
        return all_posts, None

    except Exception as exc:
        error_str = str(exc)
        # Apify surfaces credit exhaustion as an HTTP 402 or an error type string.
        if (
            getattr(exc, "status_code", None) == 402
            or "insufficient-credits" in error_str.lower()
            or "credits" in error_str.lower()
        ):
            message = f"Apify account has insufficient credits. The scrape did not run. ({error_str})"
        else:
            message = f"Apify scrape failed with an unexpected error: {error_str}"
        print(f"Critical error in batch scraping: {message}")
        return [], message
