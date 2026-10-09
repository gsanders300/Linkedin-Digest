from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# Runs are daily, but look back further so a late or retried run doesn't miss posts
# published since the previous run. seen_posts.json filters out the overlap.
LOOKBACK_HOURS = 36


def filter_new_posts(all_posts: list[dict]) -> list[dict]:
    """Return only posts published within the last LOOKBACK_HOURS (Eastern time)."""
    now_et = datetime.now(EASTERN)
    cutoff_time = now_et - timedelta(hours=LOOKBACK_HOURS)

    print(
        f"Filtering {len(all_posts)} posts for last {LOOKBACK_HOURS} hours "
        f"(since {cutoff_time.strftime('%b %d, %I:%M %p %Z')})..."
    )

    new_posts = [
        post
        for post in all_posts
        if (post_date := parse_post_date(post.get("published_date", ""))) is not None
        and post_date >= cutoff_time
    ]

    return new_posts


def parse_post_date(date_string: str) -> datetime | None:
    """Parse an ISO 8601 date string as an Eastern timezone-aware datetime."""
    if not date_string:
        return None
    try:
        clean = date_string.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EASTERN)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def convert_to_eastern(utc_datetime_str: str) -> str:
    """Format a UTC ISO 8601 string as a human-readable Eastern time string."""
    dt_et = parse_post_date(utc_datetime_str)
    if dt_et is None:
        return utc_datetime_str
    return dt_et.strftime("%b %d, %Y at %I:%M %p %Z")


def extract_post_title(post_text: str, max_length: int = 100) -> str:
    """Return the first non-empty line of post_text, truncated to max_length chars."""
    if not post_text:
        return "Untitled Post"
    first_line = post_text.split("\n")[0].strip()
    if not first_line:
        return "Untitled Post"
    if len(first_line) > max_length:
        return first_line[:max_length] + "..."
    return first_line
