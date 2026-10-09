import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.seen_file = self.data_dir / "seen_posts.json"
        self.data_dir_patch = patch.object(storage, "DATA_DIR", self.data_dir)
        self.seen_file_patch = patch.object(storage, "SEEN_POSTS_FILE", self.seen_file)
        self.data_dir_patch.start()
        self.seen_file_patch.start()

    def tearDown(self) -> None:
        self.seen_file_patch.stop()
        self.data_dir_patch.stop()
        self.temp_dir.cleanup()

    def test_invalid_state_shape_is_ignored(self) -> None:
        self.seen_file.write_text('["not", "an", "object"]', encoding="utf-8")
        self.assertEqual(storage.load_seen_ids(), set())

    def test_save_writes_valid_state_and_cleans_temporary_file(self) -> None:
        storage.save_seen_ids({"one", "two"})

        payload = json.loads(self.seen_file.read_text(encoding="utf-8"))
        self.assertEqual(set(payload["post_ids"]), {"one", "two"})
        self.assertIn("last_updated", payload)
        self.assertEqual(list(self.data_dir.glob(".seen_posts.*.tmp")), [])

    def test_save_retains_existing_order_when_trimming(self) -> None:
        self.seen_file.write_text(
            json.dumps({"post_ids": ["old", "middle"]}), encoding="utf-8"
        )
        with patch.object(storage, "MAX_SEEN_IDS", 3):
            storage.save_seen_ids({"old", "middle", "new"})

        payload = json.loads(self.seen_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["post_ids"][:2], ["old", "middle"])
        self.assertEqual(payload["post_ids"][-1], "new")

    def test_profiles_skip_non_profile_lines_and_duplicates(self) -> None:
        profiles_file = self.data_dir / "profiles.txt"
        profiles_file.write_text(
            "# comment\n\n"
            "https://www.linkedin.com/in/a/\n"
            "https://www.linkedin.com/in/a\n"
            "https://www.linkedin.com/company/x/\n"
            "https://www.linkedin.com/in/b/\n",
            encoding="utf-8",
        )
        with patch.object(storage, "PROFILES_FILE", profiles_file):
            self.assertEqual(
                storage.load_profiles(),
                ["https://www.linkedin.com/in/a/", "https://www.linkedin.com/in/b/"],
            )

    def test_profiles_file_without_urls_raises(self) -> None:
        profiles_file = self.data_dir / "profiles.txt"
        profiles_file.write_text(
            "# https://www.linkedin.com/in/a/\n", encoding="utf-8"
        )
        with patch.object(storage, "PROFILES_FILE", profiles_file):
            with self.assertRaises(ValueError):
                storage.load_profiles()


if __name__ == "__main__":
    unittest.main()
