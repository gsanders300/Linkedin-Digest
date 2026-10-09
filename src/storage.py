import json
import os
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"
PROFILES_FILE = CONFIG_DIR / "profiles.txt"

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_POSTS_FILE = DATA_DIR / "seen_posts.json"

# Maximum number of post IDs to retain; covers ~35 days of maximum activity.
MAX_SEEN_IDS = 500


def load_profiles() -> list[str]:
    """Return the list of LinkedIn profile URLs from config/profiles.txt.

    Blank lines and lines starting with '#' are ignored, other non-profile lines are
    skipped with a warning, and duplicates (ignoring a trailing slash) are dropped.
    Raises FileNotFoundError if the profiles file does not exist, and ValueError if
    it contains no profile URLs, so a bad file fails loudly instead of quietly
    producing empty digests.
    """
    if not PROFILES_FILE.exists():
        raise FileNotFoundError(f"Profiles file not found: {PROFILES_FILE}")
    profiles: dict[str, str] = {}
    for line in PROFILES_FILE.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if "linkedin.com/in/" not in url:
            print(f"Warning: skipping non-profile line in {PROFILES_FILE.name}: {url}")
            continue
        profiles.setdefault(url.rstrip("/"), url)
    if not profiles:
        raise ValueError(f"No LinkedIn profile URLs found in {PROFILES_FILE}.")
    return list(profiles.values())


def _read_seen_post_ids() -> list[str]:
    """Read and validate the ordered post-ID list from disk."""
    if not SEEN_POSTS_FILE.exists():
        return []
    try:
        data = json.loads(SEEN_POSTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("root value is not an object")
        post_ids = data.get("post_ids", [])
        if not isinstance(post_ids, list):
            raise TypeError("post_ids is not a list")
        return [
            post_id.strip()
            for post_id in post_ids
            if isinstance(post_id, str) and post_id.strip()
        ]
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        print(f"Warning: could not read {SEEN_POSTS_FILE}: {exc}. Starting fresh.")
        return []


def load_seen_ids() -> set[str]:
    """Return the set of post IDs that have already been emailed."""
    return set(_read_seen_post_ids())


def save_seen_ids(seen_ids: set[str]) -> None:
    """Persist seen post IDs to disk, capping at MAX_SEEN_IDS most-recent entries.

    Ordering is not guaranteed for a plain set, so we preserve insertion order by
    reading the existing list, appending new IDs, deduplicating, then trimming.
    """
    # Read the existing ordered list so we can append to the end and trim from the front.
    existing = _read_seen_post_ids()

    # Use an OrderedDict as an ordered set: existing entries first, new ones appended.
    ordered: dict[str, None] = OrderedDict.fromkeys(existing)
    for post_id in seen_ids:
        ordered[post_id] = None  # Adds at the end if not already present.

    # Trim to the most recent MAX_SEEN_IDS entries.
    trimmed = list(ordered.keys())[-MAX_SEEN_IDS:]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "post_ids": trimmed,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    )

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=DATA_DIR,
            prefix=".seen_posts.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        temp_path.replace(SEEN_POSTS_FILE)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print(f"Saved {len(trimmed)} seen post IDs to {SEEN_POSTS_FILE}.")
