import html as html_lib
import os
import random
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from utils import EASTERN, convert_to_eastern, extract_post_title, parse_post_date

SMTP_TIMEOUT_SECONDS = 30
SMTP_MAX_ATTEMPTS = 3
SMTP_RETRY_BASE_DELAY = 2.0


def _build_error_banner(scrape_error: str) -> str:
    return f"""
    <div style="background:#fff0f0;border-left:4px solid #cc0000;border-radius:0 8px 8px 0;
                padding:16px 20px;margin:20px 0;">
        <div style="color:#cc0000;font-weight:bold;font-size:15px;margin-bottom:6px;">
            Scraper Error
        </div>
        <div style="color:#333;font-size:14px;line-height:1.5;">{html_lib.escape(scrape_error)}</div>
    </div>"""


def _build_gemini_error_banner(gemini_error: str) -> str:
    return f"""
    <div style="background:#fff8e1;border-left:4px solid #f57f17;border-radius:0 8px 8px 0;
                padding:16px 20px;margin:20px 0;">
        <div style="color:#f57f17;font-weight:bold;font-size:15px;margin-bottom:6px;">
            Summarization Warning
        </div>
        <div style="color:#333;font-size:14px;line-height:1.5;">{html_lib.escape(gemini_error)}</div>
    </div>"""


def _build_no_posts_notice() -> str:
    return """
    <div style="background:#f0f4f8;border-left:4px solid #90a4ae;border-radius:0 8px 8px 0;
                padding:16px 20px;margin:20px 0;color:#546e7a;font-size:14px;">
        No new posts from your LinkedIn network since the last digest.
    </div>"""


def _build_usage_footer(usage: dict) -> str:
    used = usage["used_usd"]
    limit = usage["limit_usd"]
    percent = usage["percent"]

    # Clamp the progress bar fill to 100% so it never overflows the container.
    bar_pct = min(percent, 100.0)

    # Choose a color that reflects how close the account is to its limit.
    if percent >= 90:
        bar_color = "#cc0000"
    elif percent >= 70:
        bar_color = "#e65100"
    else:
        bar_color = "#0077B5"

    return f"""
    <div style="margin:30px 0 10px;padding:14px 20px;background:#f6f8fa;
                border-radius:8px;font-size:12px;color:#546e7a;">
        <div style="margin-bottom:6px;">
            <strong>Apify usage this month:</strong>
            ${used:.2f} of ${limit:.2f} &nbsp;({percent:.1f}%)
        </div>
        <div style="background:#dce3ea;border-radius:4px;height:6px;overflow:hidden;">
            <div style="width:{bar_pct:.1f}%;height:6px;background:{bar_color};
                        border-radius:4px;"></div>
        </div>
    </div>"""


def _build_post_card(post: dict) -> str:
    title = html_lib.escape(extract_post_title(post.get("text", "")))
    posted_at = html_lib.escape(convert_to_eastern(post.get("published_date", "")))
    summary = html_lib.escape(post.get("summary", ""))
    post_url = html_lib.escape(post.get("post_url", ""))
    return f"""<div class="post">
        <div class="post-title">{title}</div>
        <div class="post-meta">{posted_at}</div>
        <div class="summary">{summary}</div>
        <a href="{post_url}" class="link">Read on LinkedIn &rarr;</a>
    </div>"""


def _author_key(post: dict) -> str:
    """Consistent lowercase key for grouping posts by author name."""
    return post.get("profile_name", "").lower()


# Sentinel used so posts with missing or unparseable dates sort before dated ones.
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _post_time_key(post: dict) -> datetime:
    """Chronological sort key (oldest first) for a post."""
    return parse_post_date(post.get("published_date", "")) or _MIN_DT


def _grouped_authors(posts: list[dict]) -> list[list[dict]]:
    """Group posts by author, randomize author order, and sort each author's
    posts chronologically (oldest first).

    The author ordering is shuffled on every call so the digest presents a
    different author sequence each time it is built.
    """
    groups: dict[str, list[dict]] = {}
    for post in posts:
        groups.setdefault(_author_key(post), []).append(post)

    author_groups = list(groups.values())
    random.shuffle(author_groups)
    for group in author_groups:
        group.sort(key=_post_time_key)
    return author_groups


def build_html_digest(
    posts: list[dict],
    scrape_error: Optional[str] = None,
    gemini_error: Optional[str] = None,
    usage: Optional[dict] = None,
    job_start: Optional[datetime] = None,
    job_end: Optional[datetime] = None,
) -> str:
    now_et = job_start if job_start is not None else datetime.now(EASTERN)

    parts: list[str] = [
        f"""<html><head><style>
        body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; background: #f6f8fa; }}
        .header {{ background: #0077B5; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
        .author-section {{ margin: 24px 0 0; }}
        .author-heading {{ background: #e8f0f7; padding: 10px 16px; border-radius: 6px;
                           font-size: 15px; font-weight: bold; color: #004f80; }}
        .author-heading a {{ color: #004f80; text-decoration: none; }}
        .author-heading a:hover {{ text-decoration: underline; }}
        .post {{ background: white; padding: 20px; margin: 10px 0 10px 12px;
                 border-left: 4px solid #0077B5; border-radius: 0 8px 8px 0; }}
        .post-title {{ font-size: 16px; color: #0077B5; font-weight: bold; }}
        .post-meta {{ font-size: 12px; color: #666; margin-bottom: 10px; }}
        .summary {{ font-size: 14px; margin-bottom: 15px; line-height: 1.5; }}
        .link {{ color: #0077B5; text-decoration: none; font-weight: bold; }}
        .content {{ padding: 0 20px 20px; }}
    </style></head><body>
    <div class="header">
        <h1>LinkedIn Daily Digest</h1>
        <p>{now_et.strftime("%B %d, %Y")}</p>
        <p style="font-size:13px;margin:4px 0 0;">Started: {job_start.strftime("%I:%M:%S %p %Z") if job_start is not None else "N/A"}</p>
        <p style="font-size:13px;margin:2px 0 0;">Completed: {job_end.strftime("%I:%M:%S %p %Z") if job_end is not None else "N/A"}</p>
    </div>
    <div class="content">"""
    ]

    if scrape_error:
        parts.append(_build_error_banner(scrape_error))

    if gemini_error:
        parts.append(_build_gemini_error_banner(gemini_error))

    if posts:
        # Authors in random order; posts within each author chronological.
        for group_posts in _grouped_authors(posts):
            # Preserve original casing for display from the first post in the group.
            author_name = group_posts[0].get("profile_name", "")
            profile_url = html_lib.escape(group_posts[0].get("profile_url", ""))
            safe_author = html_lib.escape(author_name)
            author_link = (
                f'<a href="{profile_url}">{safe_author}</a>'
                if profile_url
                else safe_author
            )
            parts.append(
                f"""<div class="author-section">
                <div class="author-heading">{author_link}</div>"""
            )
            for post in group_posts:
                parts.append(_build_post_card(post))
            parts.append("</div>")
    elif not scrape_error:
        parts.append(_build_no_posts_notice())

    if usage is not None:
        parts.append(_build_usage_footer(usage))

    parts.append("</div></body></html>")
    return "".join(parts)


def build_plain_digest(
    posts: list[dict],
    scrape_error: Optional[str] = None,
    gemini_error: Optional[str] = None,
    usage: Optional[dict] = None,
    job_start: Optional[datetime] = None,
    job_end: Optional[datetime] = None,
) -> str:
    now_et = job_start if job_start is not None else datetime.now(EASTERN)
    lines: list[str] = [
        "LinkedIn Daily Digest",
        now_et.strftime("%B %d, %Y"),
        f"Started: {job_start.strftime('%I:%M:%S %p %Z') if job_start is not None else 'N/A'}",
        f"Completed: {job_end.strftime('%I:%M:%S %p %Z') if job_end is not None else 'N/A'}",
        "",
    ]

    if scrape_error:
        lines += ["SCRAPER ERROR", scrape_error, ""]

    if gemini_error:
        lines += ["SUMMARIZATION WARNING", gemini_error, ""]

    if posts:
        for group_posts in _grouped_authors(posts):
            author_name = group_posts[0].get("profile_name", "")
            lines.append(f"── {author_name} ──")
            for post in group_posts:
                title = extract_post_title(post.get("text", ""))
                posted_at = convert_to_eastern(post.get("published_date", ""))
                summary = post.get("summary", "")
                post_url = post.get("post_url", "")
                lines += [
                    f"  {title}",
                    f"  {posted_at}",
                    f"  {summary}",
                    f"  {post_url}",
                    "",
                ]
    elif not scrape_error:
        lines += [
            "No new posts from your LinkedIn network since the last digest.",
            "",
        ]

    if usage is not None:
        lines += [
            f"Apify usage this month: ${usage['used_usd']:.2f} of ${usage['limit_usd']:.2f} ({usage['percent']:.1f}%)",
            "",
        ]

    return "\n".join(lines)


def _subject_line(posts: list[dict], scrape_error: Optional[str]) -> str:
    if scrape_error:
        return "LinkedIn Digest - Scraper Error"
    if not posts:
        return "LinkedIn Digest - No New Posts Today"
    count = len(posts)
    return f"LinkedIn Digest - {count} New Post{'s' if count != 1 else ''}"


def send_digest_email(
    posts: list[dict],
    scrape_error: Optional[str] = None,
    gemini_error: Optional[str] = None,
    usage: Optional[dict] = None,
    job_start: Optional[datetime] = None,
    job_end: Optional[datetime] = None,
) -> bool:
    """Send the digest email.

    Returns True only if the message was sent successfully; returns False if
    credentials are missing or the send failed. The caller uses this to decide
    whether the included posts may be marked as delivered.
    """
    sender = os.getenv("GMAIL_ADDRESS") or ""
    password = os.getenv("GMAIL_APP_PASSWORD") or ""
    recipient = os.getenv("RECIPIENT_EMAIL") or ""

    if not sender or not password or not recipient:
        print("Error: Missing email credentials in environment variables.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = _subject_line(posts, scrape_error)
        msg["From"] = sender
        msg["To"] = recipient

        # Per MIME spec, plain text comes first; HTML comes last and is preferred.
        msg.attach(
            MIMEText(
                build_plain_digest(
                    posts,
                    scrape_error=scrape_error,
                    gemini_error=gemini_error,
                    usage=usage,
                    job_start=job_start,
                    job_end=job_end,
                ),
                "plain",
            )
        )
        msg.attach(
            MIMEText(
                build_html_digest(
                    posts,
                    scrape_error=scrape_error,
                    gemini_error=gemini_error,
                    usage=usage,
                    job_start=job_start,
                    job_end=job_end,
                ),
                "html",
            )
        )
    except Exception as exc:
        print(f"Error: Failed to build digest email: {exc}")
        return False

    for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
        try:
            with smtplib.SMTP_SSL(
                "smtp.gmail.com", 465, timeout=SMTP_TIMEOUT_SECONDS
            ) as server:
                server.login(sender, password)
                server.send_message(msg)
            print(f"Email sent: {msg['Subject']}")
            return True
        except smtplib.SMTPAuthenticationError as exc:
            print(f"Error: Gmail authentication failed: {exc}")
            return False
        except (smtplib.SMTPException, OSError) as exc:
            if attempt >= SMTP_MAX_ATTEMPTS:
                print(f"Error: Failed to send digest email after {attempt} attempts: {exc}")
                return False
            delay = SMTP_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(
                f"Warning: Email attempt {attempt}/{SMTP_MAX_ATTEMPTS} failed; "
                f"retrying in {delay:.1f}s. Error: {exc}"
            )
            time.sleep(delay)
        except Exception as exc:
            print(f"Error: Failed to send digest email: {exc}")
            return False

    return False
