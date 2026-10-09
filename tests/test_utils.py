import unittest
from datetime import datetime, timedelta, timezone

from utils import LOOKBACK_HOURS, filter_new_posts


def _post_hours_ago(hours: float) -> dict:
    published = datetime.now(timezone.utc) - timedelta(hours=hours)
    return {"id": str(hours), "published_date": published.isoformat()}


class FilterNewPostsTests(unittest.TestCase):
    def test_keeps_posts_inside_lookback_and_drops_older_ones(self) -> None:
        posts = [
            _post_hours_ago(30),
            _post_hours_ago(LOOKBACK_HOURS - 1),
            _post_hours_ago(LOOKBACK_HOURS + 1),
        ]
        kept = filter_new_posts(posts)
        self.assertEqual(
            [post["id"] for post in kept], ["30", str(LOOKBACK_HOURS - 1)]
        )


if __name__ == "__main__":
    unittest.main()
