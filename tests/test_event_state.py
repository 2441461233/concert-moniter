import json
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from lib import store


ROOT = Path(__file__).resolve().parents[1]


class EventStateTests(unittest.TestCase):
    def test_python_and_browser_agree_at_calendar_and_sale_boundaries(self):
        events = [
            {"show_date": "2026-09-16", "sale_status": "on_sale"},
            {"show_date": "2026-09-17", "sale_status": "sold_out"},
            {"show_date": "2026-10-17", "sale_status": "scheduled", "sale_time": "2026-09-17T20:00+09:00"},
            {"show_date": "2026-12-23", "sale_status": "scheduled", "sale_time": "2026-09-07T17:00+09:00", "sale_end_time": "2026-09-15T23:59+09:00"},
            {"show_date": "2026-10-01", "sale_status": "cancelled"},
            {"show_date": "2026-10-01", "sale_time": "2026-09-17 19:00"},
            {"show_date": "", "sale_status": "announced"},
        ]
        scenarios = [
            ("2026-09-17T18:59:59+08:00", ["ended", "on_sale", "upcoming", "upcoming", "ended", "upcoming", "upcoming"]),
            ("2026-09-17T19:00:00+08:00", ["ended", "on_sale", "on_sale", "upcoming", "ended", "on_sale", "upcoming"]),
            ("2026-09-18T00:00:00+08:00", ["ended", "ended", "on_sale", "upcoming", "ended", "on_sale", "upcoming"]),
        ]
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend regression tests")
        for clock, expected in scenarios:
            with self.subTest(clock=clock), mock.patch.object(store, "local_now", return_value=datetime.fromisoformat(clock)):
                self.assertEqual(expected, [store.derive_status(e) for e in events])
            script = "const s=require('./site/event-state.js');const i=JSON.parse(require('fs').readFileSync(0,'utf8'));console.log(JSON.stringify(i.events.map(e=>s.eventStatus(e,new Date(i.clock)))));"
            result = subprocess.run([node, "-e", script], input=json.dumps({"events": events, "clock": clock}), text=True, capture_output=True, cwd=ROOT, check=True)
            self.assertEqual(expected, json.loads(result.stdout))

    def test_browser_snapshot_and_page_roll_over_without_a_new_build(self):
        subprocess.run([shutil.which("node"), str(ROOT / "tests" / "frontend_runtime.cjs")], cwd=ROOT, check=True, capture_output=True, text=True)

    def test_lottery_is_only_active_inside_its_window(self):
        event = {"show_date": "2026-12-23", "sale_status": "scheduled", "sale_time": "2026-09-07T17:00+09:00", "sale_end_time": "2026-09-15T23:59+09:00"}
        for clock, expected in [("2026-09-07T15:59:59+08:00", "upcoming"), ("2026-09-07T16:00:00+08:00", "on_sale"), ("2026-09-15T22:59:00+08:00", "upcoming")]:
            with self.subTest(clock=clock), mock.patch.object(store, "local_now", return_value=datetime.fromisoformat(clock)):
                self.assertEqual(expected, store.derive_status(event))


if __name__ == "__main__":
    unittest.main()
