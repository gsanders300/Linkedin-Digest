import unittest

from apify_scraper import _normalise_post_item, _safe_int


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


if __name__ == "__main__":
    unittest.main()
