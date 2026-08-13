import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "site" / "index.html").read_text(encoding="utf-8")


def function_body(name):
    match = re.search(
        rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(.*?)\n\}}",
        INDEX,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("missing JavaScript function: %s" % name)
    return match.group(1)


class FrontendContractTests(unittest.TestCase):
    def test_refresh_notes_require_matching_owner_marker(self):
        self.assertIn('const REFRESH_OWNER_KEY = "cm-full-refresh-owner-id";', INDEX)
        self.assertIn("if((D.notes||[]).length && ownsPublishedRefresh())", INDEX)
        owner_body = function_body("ownsPublishedRefresh")
        self.assertIn("D.full_refresh_id", owner_body)
        self.assertIn("loadRefreshOwnerId()===publishedId", owner_body)

    def test_only_new_task_creator_can_become_owner(self):
        self.assertIn("is_owner:payload.already_running===false", INDEX)
        self.assertIn("if(job.is_owner===true) saveRefreshOwnerId(job.job_id);", INDEX)

    def test_artist_badges_count_active_concerts_not_rumors(self):
        body = function_body("activeConcertItems")
        self.assertIn("D.on_sale", body)
        self.assertIn("D.upcoming", body)
        self.assertNotIn("D.rumors", body)
        self.assertIn("activeConcertItems().filter", INDEX)
        self.assertIn('const ru = (D.rumors||[]).filter(keep);', INDEX)


if __name__ == "__main__":
    unittest.main()
