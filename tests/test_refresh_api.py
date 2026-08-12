import unittest
from unittest import mock

from api import refresh


JOB_ID = "0123456789abcdef01234567"


def run(status="in_progress", conclusion=None, job_id=JOB_ID):
    return {
        "display_title": "Full refresh · " + job_id,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-08-12T12:00:00Z",
        "run_started_at": "2026-08-12T12:00:05Z",
        "updated_at": "2026-08-12T12:03:00Z",
        "html_url": "https://github.com/example/actions/runs/1",
    }


class RefreshDispatchTests(unittest.TestCase):
    def test_active_run_is_reused(self):
        with mock.patch.object(refresh, "_github_request") as request:
            result = refresh.dispatch_full_refresh([run()])
        request.assert_not_called()
        self.assertTrue(result["already_running"])
        self.assertEqual(JOB_ID, result["job_id"])
        self.assertEqual("in_progress", result["status"])

    def test_new_job_dispatches_workflow(self):
        with mock.patch.object(refresh.secrets, "token_hex", return_value=JOB_ID), mock.patch.object(
            refresh, "_github_request", return_value={
                "workflow_run_id": 42,
                "html_url": "https://github.com/example/actions/runs/42",
            }
        ) as request:
            result = refresh.dispatch_full_refresh([])
        self.assertFalse(result["already_running"])
        self.assertEqual("queued", result["status"])
        self.assertEqual(42, result["run_id"])
        request.assert_called_once()
        method, path, payload = request.call_args.args
        self.assertEqual("POST", method)
        self.assertTrue(path.endswith("/dispatches"))
        self.assertEqual(JOB_ID, payload["inputs"]["request_id"])

    def test_status_lifecycle(self):
        queued = refresh.run_status(JOB_ID, [])
        self.assertEqual("queued", queued["status"])

        working = refresh.run_status(JOB_ID, [run()])
        self.assertEqual("researching", working["stage"])

        complete = refresh.run_status(JOB_ID, [run("completed", "success")])
        self.assertEqual("completed", complete["status"])
        self.assertEqual("publishing", complete["stage"])

        failed = refresh.run_status(JOB_ID, [run("completed", "failure")])
        self.assertEqual("failed", failed["status"])
        self.assertIn("failure", failed["error"])

    def test_post_requires_same_origin(self):
        self.assertFalse(refresh.request_is_same_origin({"Host": "example.com"}, True))
        self.assertTrue(refresh.request_is_same_origin({
            "Host": "example.com",
            "Origin": "https://example.com",
            "Sec-Fetch-Site": "same-origin",
        }, True))
        self.assertFalse(refresh.request_is_same_origin({
            "Host": "example.com",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        }, True))

    def test_trigger_requires_matching_secret(self):
        headers = {
            "Host": "example.com",
            "Origin": "https://example.com",
            "Sec-Fetch-Site": "same-origin",
            "Authorization": "Bearer correct horse",
        }
        with mock.patch.dict(refresh.os.environ, {"REFRESH_SECRET": "correct horse"}):
            self.assertTrue(refresh.request_can_trigger(headers))
            self.assertFalse(refresh.request_can_trigger({**headers, "Authorization": "Bearer wrong"}))

    def test_status_token_is_job_scoped_and_requires_secret(self):
        other_job = "fedcba9876543210fedcba98"
        with mock.patch.dict(refresh.os.environ, {"REFRESH_SECRET": "correct horse"}):
            token = refresh.status_token_for(JOB_ID)
            self.assertTrue(refresh.request_can_read_status(JOB_ID, token))
            self.assertFalse(refresh.request_can_read_status(other_job, token))
            self.assertFalse(refresh.request_can_read_status(JOB_ID, ""))
        with mock.patch.dict(refresh.os.environ, {}, clear=True):
            with self.assertRaises(refresh.RefreshConfigurationError):
                refresh.status_token_for(JOB_ID)


if __name__ == "__main__":
    unittest.main()
