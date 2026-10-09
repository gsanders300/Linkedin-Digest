import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import apify_scraper
from apify_scraper import _normalise_post_item, _safe_int


class ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _client_returning(items: list) -> MagicMock:
    client = MagicMock()
    client.actor.return_value.call.return_value = SimpleNamespace(
        status="SUCCEEDED", default_dataset_id="dataset"
    )
    client.dataset.return_value.list_items.return_value.items = items
    return client


class ApifyNormalisationTests(unittest.TestCase):
    def test_falls_back_to_final_url_segment_without_trailing_slash(self) -> None:
        post = _normalise_post_item(
            {
                "id": None,
                "linkedinUrl": "https://www.linkedin.com/feed/update/urn:li:activity:123?x=1",
                "author": {"name": "Example"},
                "content": "Post text",
            }
        )

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post["id"], "urn:li:activity:123")
        self.assertEqual(
            post["post_url"],
            "https://www.linkedin.com/feed/update/urn:li:activity:123/",
        )

    def test_malformed_nested_values_use_safe_defaults(self) -> None:
        post = _normalise_post_item(
            {
                "id": "abc",
                "linkedinUrl": 42,
                "author": None,
                "postedAt": {"date": None},
                "engagement": None,
                "content": None,
            }
        )

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post["profile_name"], "LinkedIn User")
        self.assertEqual(post["post_url"], "")
        self.assertEqual(post["published_date"], "")
        self.assertEqual(post["text"], "")
        self.assertEqual(post["likes"], 0)

    def test_non_post_items_are_ignored(self) -> None:
        self.assertIsNone(_normalise_post_item({"type": "reaction", "id": "abc"}))

    def test_safe_int_handles_formatted_and_invalid_values(self) -> None:
        self.assertEqual(_safe_int("1,234"), 1234)
        self.assertEqual(_safe_int(None), 0)
        self.assertEqual(_safe_int("not-a-number"), 0)


class ScrapeProfilesTests(unittest.TestCase):
    def _scrape(self, client: MagicMock) -> tuple[list, str | None]:
        with patch.object(apify_scraper, "_get_client", return_value=client):
            return apify_scraper.scrape_profiles(["https://www.linkedin.com/in/a/"])

    def test_posts_with_readable_dates_succeed(self) -> None:
        client = _client_returning(
            [{"id": "1", "content": "Hi", "postedAt": {"date": "2026-10-08T12:00:00Z"}}]
        )
        posts, error = self._scrape(client)
        self.assertIsNone(error)
        self.assertEqual(len(posts), 1)

    def test_actor_is_asked_for_posts_since_lookback_cutoff(self) -> None:
        client = _client_returning([])
        self._scrape(client)

        run_input = client.actor.return_value.call.call_args.kwargs["run_input"]
        self.assertNotIn("postedLimit", run_input)
        cutoff = datetime.fromisoformat(run_input["postedLimitDate"])
        expected = datetime.now(timezone.utc) - timedelta(
            hours=apify_scraper.LOOKBACK_HOURS
        )
        self.assertLess(abs(cutoff - expected), timedelta(minutes=1))

    def test_no_readable_dates_is_reported_as_error(self) -> None:
        client = _client_returning(
            [{"id": "1", "content": "Hi", "postedAt": {"date": "3 hours ago"}}]
        )
        posts, error = self._scrape(client)
        self.assertEqual(posts, [])
        self.assertIn("readable publish date", error or "")

    def test_http_402_is_reported_as_insufficient_credits(self) -> None:
        client = MagicMock()
        client.actor.return_value.call.side_effect = ApiError("Payment required", 402)
        _, error = self._scrape(client)
        self.assertIn("insufficient credits", error or "")

    def test_402_in_message_text_is_not_a_credit_error(self) -> None:
        client = MagicMock()
        client.actor.return_value.call.side_effect = ApiError("Run abc402def failed")
        _, error = self._scrape(client)
        self.assertIn("unexpected error", error or "")


if __name__ == "__main__":
    unittest.main()
