import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from apify_scraper import scrape_profiles, get_apify_usage_stats
from ai_summarizer import summarize_posts
from email_sender import send_digest_email
from storage import load_profiles, load_seen_ids, save_seen_ids
from utils import filter_new_posts

EASTERN = ZoneInfo("America/New_York")


def should_run_now() -> bool:
    # Allow manual bypass via environment variable.
    if os.getenv("FORCE_RUN") == "true":
        print("Force run enabled. Bypassing time checks.")
        return True

    # The GitHub Actions cron schedule controls when this job runs.
    # No redundant in-process time check is needed; always proceed.
    return True


def main() -> None:
    job_start = datetime.now(EASTERN)

    if not should_run_now():
        print("Not time to run yet.")
        return

    profile_urls = load_profiles()
    seen_ids = load_seen_ids()

    print(
        f"Loaded {len(profile_urls)} profile URLs. "
        f"Tracking {len(seen_ids)} previously seen post IDs."
    )

    all_posts, scrape_error = scrape_profiles(profile_urls)

    # Always fetch usage stats so every email includes the credit meter.
    usage = get_apify_usage_stats()

    print(f"Total posts retrieved: {len(all_posts)}")
    recent_posts = filter_new_posts(all_posts)

    print(f"Posts from last 24 hours: {len(recent_posts)}")

    # Exclude posts that have already been emailed to avoid duplicate digests.
    new_posts = [p for p in recent_posts if p["id"] not in seen_ids]
    skipped = len(recent_posts) - len(new_posts)
    if skipped:
        print(f"Skipped {skipped} already-seen post(s).")

    summarized: list[dict] = []
    gemini_error = None
    if new_posts:
        print("Summarizing posts...")
        summarized, gemini_error = summarize_posts(new_posts)

    job_end = datetime.now(EASTERN)
    print("Sending digest email...")
    email_sent = send_digest_email(
        summarized,
        scrape_error=scrape_error,
        gemini_error=gemini_error,
        usage=usage,
        job_start=job_start,
        job_end=job_end,
    )

    failures: list[str] = []
    if scrape_error is not None:
        failures.append(scrape_error)
    if not email_sent:
        failures.append("Digest email was not sent.")
    if failures:
        # The error digest has already been attempted. A non-zero exit now makes the
        # failure visible in GitHub Actions and prevents the persistence step.
        raise RuntimeError(" | ".join(failures))

    # Persist only after both scraping and email delivery have succeeded.
    if new_posts:
        emailed_ids = {p["id"] for p in new_posts}
        save_seen_ids(seen_ids | emailed_ids)


if __name__ == "__main__":
    main()
