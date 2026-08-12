import json
import unittest
from pathlib import Path
from unittest import mock

from api import refresh


ROOT = Path(__file__).resolve().parents[1]
BUSINESS_DIRS = ("config", "data", "research", "site")


def snapshot_business_files():
    """Return the repository-backed state a live refresh must not mutate."""
    snapshot = {}
    for name in BUSINESS_DIRS:
        base = ROOT / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if ".cache" in relative.parts or "__pycache__" in relative.parts:
                continue
            snapshot[str(relative)] = path.read_bytes()
    return snapshot


class RefreshSnapshotTests(unittest.TestCase):
    def setUp(self):
        old_payload = refresh._CACHE_PAYLOAD
        old_at = refresh._CACHE_AT
        refresh._CACHE_PAYLOAD = None
        refresh._CACHE_AT = 0.0
        self.addCleanup(setattr, refresh, "_CACHE_PAYLOAD", old_payload)
        self.addCleanup(setattr, refresh, "_CACHE_AT", old_at)

    def test_refresh_snapshot_is_isolated_and_returns_fresh_status(self):
        before_files = snapshot_business_files()
        previous_last_run = json.loads((ROOT / "data" / "meta.json").read_text(
            encoding="utf-8"
        )).get("last_run")

        path_state = {
            (module, attr): getattr(module, attr)
            for module, attrs in (
                (refresh.monitor, ("ROOT", "CONFIG_PATH", "SITE_DIR", "INBOX_DIR", "ARCHIVE_DIR")),
                (refresh.monitor.store, (
                    "ROOT", "DATA_DIR", "EVENTS_PATH", "RUMORS_PATH", "META_PATH",
                    "CHANGELOG_PATH",
                )),
                (refresh.monitor.showstart.http, ("ROOT", "CACHE_DIR")),
            )
            for attr in attrs
        }

        with mock.patch.object(
            refresh.monitor.showstart,
            "collect",
            return_value=([], [], None),
        ) as collect_mock, mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("refresh test attempted a network request"),
        ):
            data, cached = refresh.refresh_snapshot()

        self.assertFalse(cached)
        self.assertRegex(
            data["last_run"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
        )
        self.assertNotEqual(previous_last_run, data["last_run"])

        source_status = data["source_status"]["showstart"]
        self.assertEqual(0, source_status["fail"])
        self.assertEqual(collect_mock.call_count, source_status["ok"])
        self.assertGreater(source_status["ok"], 0)

        self.assertEqual(before_files, snapshot_business_files())
        for (module, attr), old_value in path_state.items():
            with self.subTest(module=module.__name__, attr=attr):
                self.assertEqual(old_value, getattr(module, attr))


if __name__ == "__main__":
    unittest.main()
