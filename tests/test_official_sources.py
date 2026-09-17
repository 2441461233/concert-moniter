import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor
from lib import official, store
from scripts import full_refresh


FIXTURES = Path(__file__).parent / "fixtures" / "official"
ARTIST = {"key": "illit", "name": "ILLIT", "region": "kpop", "official_sources": [
    {"parser": "illit_nol_encore", "url": "https://nol.yanolja.com/ticket/products/26012865"},
    {"parser": "illit_japan_encore", "url": "https://illit-official.jp/news/35e24927f649"},
]}


def fetch_fixture(url, **kwargs):
    return (FIXTURES / ("illit-nol.html" if "yanolja" in url else "illit-japan.html")).read_text(), None


class OfficialSourceTests(unittest.TestCase):
    def collect(self):
        with mock.patch.object(official.http, "get", side_effect=fetch_fixture):
            return [e for source in ARTIST["official_sources"] for e in official.collect(ARTIST, source)]

    def test_collects_six_individual_shows_with_local_times_and_real_ticket_link(self):
        events = self.collect()
        self.assertEqual(["2026-10-17", "2026-10-18", "2026-12-23", "2026-12-24", "2026-12-26", "2026-12-27"], [e["show_date"] for e in events])
        self.assertEqual(["18:00", "17:00", "18:30", "18:30", "17:00", "17:00"], [e["show_time"] for e in events])
        self.assertEqual("17:00", events[2]["doors_time"])
        self.assertEqual("2026-09-17T20:00+09:00", events[0]["sale_time"])
        self.assertEqual("2026-09-15T23:59+09:00", events[2]["sale_end_time"])
        self.assertTrue(events[0]["ticket_url"].endswith("/26012865"))
        self.assertEqual(6, len({e["source_id"] for e in events}))

    def test_repeat_refresh_keeps_six_records_and_preserves_ticket_window(self):
        events = self.collect()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(store, "EVENTS_PATH", str(Path(tmp)/"events.json")):
            store.merge_events(events, "run-1")
            store.merge_events(events, "run-2")
            persisted = store.load_events()
            self.assertEqual(6, len(persisted))
            japan = next(e for e in persisted.values() if e["show_date"] == "2026-12-23")
            self.assertEqual("2026-09-15T23:59+09:00", japan["sale_end_time"])
            self.assertEqual("run-1", japan["first_seen_run"])
            self.assertEqual("run-2", japan["last_seen_run"])

    def test_empty_challenge_and_changed_page_are_not_successful_zero_results(self):
        for body in ["", "<html>Checking your browser</html>", "<h1>ILLIT</h1>"]:
            for source in ARTIST["official_sources"]:
                with self.subTest(body=body, source=source), mock.patch.object(official.http, "get", return_value=(body, None)):
                    with self.assertRaises(ValueError):
                        official.collect(ARTIST, source)

    def test_missing_one_official_source_aborts_before_any_write(self):
        def collect(artist, source, **kwargs):
            if source["parser"] == "illit_japan_encore":
                raise ValueError("changed page")
            return self.collect()[:2]
        with mock.patch.object(monitor, "load_config", return_value={"artists": [ARTIST]}), \
             mock.patch.object(official, "collect", side_effect=collect), \
             mock.patch.object(store, "merge_events") as merge, \
             mock.patch.object(monitor, "build_site") as build, \
             mock.patch.object(monitor, "save_config") as save_config:
            # Avoid invoking this test's fixture helper through the patched reader.
            with mock.patch.object(self, "collect", return_value=[{"title": "Korea"}]):
                with self.assertRaisesRegex(RuntimeError, "官方采集未完整"):
                    monitor.cmd_check(argparse.Namespace(force=True, strict_sources=True, no_inbox=True))
        merge.assert_not_called()
        build.assert_not_called()
        save_config.assert_not_called()

    def test_partial_date_parsing_cannot_silently_drop_a_show(self):
        for source, original, changed in [
            (ARTIST["official_sources"][0], "5PM(KST)", "17:00"),
            (ARTIST["official_sources"][1], "2026年12月27日(日) 開場", "2026年12月27日(日) 未発表"),
        ]:
            body, _ = fetch_fixture(source["url"])
            self.assertIn(original, body)
            with self.subTest(source=source), mock.patch.object(official.http, "get", return_value=(body.replace(original, changed), None)):
                with self.assertRaises(ValueError):
                    official.collect(ARTIST, source)

    def test_full_refresh_validates_official_coverage(self):
        with mock.patch.object(monitor, "load_config", return_value={"artists": [ARTIST]}):
            for status in [{}, {"ok": 1, "fail": 0}, {"ok": 2, "fail": 1}]:
                with self.subTest(status=status), self.assertRaises(full_refresh.ResearchError):
                    full_refresh.validate_showstart_coverage({"source_status": {"official": status}})
            full_refresh.validate_showstart_coverage({"source_status": {"official": {"ok": 2, "fail": 0}}})

    def test_new_official_sale_can_clear_old_lottery_deadline(self):
        old = {"source": "official", "sale_end_time": "2026-09-15T23:59+09:00"}
        new = {"source": "official", "sale_time": "2026-10-01T12:00+09:00", "sale_end_time": ""}
        self.assertEqual("", store._merge_one(old, new)["sale_end_time"])


if __name__ == "__main__":
    unittest.main()
