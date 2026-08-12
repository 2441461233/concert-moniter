import json
import unittest
from unittest import mock

from scripts import full_refresh


TOOLS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}]


def fake_fiber(index=0, status="succeeded"):
    return {
        "id": "fiber-%d" % index,
        "object": "fiber",
        "status": status,
        "context": {
            "encrypted_output": (
                "----MOONSHOT ENCRYPTED BEGIN----result-%d"
                "----MOONSHOT ENCRYPTED END----" % index
            ),
        },
        "formula": "moonshot/web-search:latest",
    }


def fake_chat_response(result, finish_reason="stop"):
    return {
        "id": "chatcmpl-test",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": json.dumps(result, ensure_ascii=False),
                "reasoning_content": "protected reasoning",
            },
            "finish_reason": finish_reason,
        }],
    }


def complete_result(events=None, rumors=None, sources=None):
    return {
        "events": [] if events is None else events,
        "rumors": [] if rumors is None else rumors,
        "sources": [] if sources is None else sources,
        "coverage": {
            "ticketing_checked": True,
            "official_checked": True,
            "china_region_checked": True,
            "rumors_checked": True,
            "summary": "All four code-executed search categories were read.",
        },
    }


def source(url="https://tickets.example.com/show/1", category="ticketing"):
    return {"category": category, "title": "Official ticket page", "url": url}


def event(url="https://tickets.example.com/show/1"):
    return {
        "url": url,
        "tour_name": "Mock Tour",
        "title": "Mock Artist · Shanghai",
        "city": "Shanghai",
        "country": "China",
        "venue": "Mock Arena",
        "show_date": "2026-12-01",
        "show_time": "19:30",
        "price": "CNY 380-1280",
        "ticket_tiers": ["CNY 380"],
        "sale_status": "upcoming",
        "sale_time": "2026-10-01 12:00",
        "confidence": "confirmed",
        "note": "Official sale page.",
    }


def executions():
    return [{
        "category": category,
        "query": "query for " + category,
        "tool_call_id": "web_search:%d" % index,
        "fiber_id": "fiber-%d" % index,
        "output": "encrypted-result-%d" % index,
    } for index, category in enumerate(full_refresh.SEARCH_CATEGORIES)]


class FullRefreshResearchTests(unittest.TestCase):
    def setUp(self):
        self.artist = {
            "key": "mock",
            "name": "Mock Artist",
            "region": "kpop",
            "aliases": ["Mock Artist", "목 아티스트"],
            "search_terms": ["Mock Artist concert"],
            "enabled": True,
        }

    def test_code_executes_exactly_four_formula_categories(self):
        requester = mock.Mock(side_effect=[fake_fiber(i) for i in range(4)])

        result = full_refresh.execute_searches(
            self.artist, "2026-08-12", requester=requester,
        )

        self.assertEqual(list(full_refresh.SEARCH_CATEGORIES), [
            item["category"] for item in result
        ])
        self.assertEqual(4, requester.call_count)
        for call, item in zip(requester.call_args_list, result):
            body = call.args[0]
            self.assertEqual("web_search", body["name"])
            self.assertEqual(item["query"], json.loads(body["arguments"])["query"])
            self.assertIn("MOONSHOT ENCRYPTED", item["output"])
            self.assertIn("2026 2027 未来", item["query"])

    def test_chat_request_has_formula_context_and_strict_schema(self):
        with mock.patch.object(full_refresh, "_existing_context", return_value={
            "events": [], "rumors": [],
        }):
            request = full_refresh.build_request(
                self.artist, "kimi-k3", "2026-08-12", TOOLS, executions(),
            )

        self.assertEqual("kimi-k3", request["model"])
        self.assertEqual("high", request["reasoning_effort"])
        self.assertEqual(16000, request["max_completion_tokens"])
        self.assertNotIn("temperature", request)
        self.assertEqual("none", request["tool_choice"])
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        assistant = request["messages"][2]
        tool_messages = request["messages"][3:]
        self.assertEqual(4, len(assistant["tool_calls"]))
        self.assertEqual(4, len(tool_messages))
        self.assertEqual(
            [call["id"] for call in assistant["tool_calls"]],
            [message["tool_call_id"] for message in tool_messages],
        )
        self.assertTrue(all(message["role"] == "tool" for message in tool_messages))

    def test_formula_tool_definition_is_loaded_and_checked(self):
        with mock.patch.object(
            full_refresh, "_moonshot_request", return_value={"tools": TOOLS},
        ) as request:
            self.assertEqual(TOOLS, full_refresh.load_formula_tools())
        request.assert_called_once_with(
            "GET", "/formulas/moonshot/web-search:latest/tools",
        )

    def test_grounded_result_injects_artist_identity(self):
        requester = mock.Mock(return_value=fake_chat_response(complete_result(
            events=[event()], sources=[source()],
        )))
        search_requester = mock.Mock(side_effect=[fake_fiber(i) for i in range(4)])

        value = full_refresh.research_artist(
            self.artist, "kimi-k3", "2026-08-12", TOOLS,
            requester=requester,
            search_requester=search_requester,
            url_checker=lambda url: True,
            retries=1,
        )

        self.assertEqual(1, requester.call_count)
        self.assertEqual(4, search_requester.call_count)
        self.assertEqual("mock", value["events"][0]["artist_key"])
        self.assertEqual("Mock Artist", value["events"][0]["artist_name"])
        self.assertEqual("research", value["events"][0]["source"])
        self.assertEqual([], value["warnings"])
        self.assertEqual("ticketing", value["sources"][0]["category"])
        self.assertEqual(4, len(value["searches"]))
        self.assertNotIn("output", value["searches"][0])

    def test_unmatched_urls_and_imprecise_rumor_dates_are_discarded(self):
        bad_rumor = {
            "headline": "Vague date", "detail": "", "source_name": "forum",
            "url": "https://tickets.example.com/show/1", "credibility": "low",
            "posted_at": "2026-08",
        }
        result = complete_result(
            events=[event("https://invented.example/event")],
            rumors=[bad_rumor],
            sources=[source()],
        )
        value, sources, warnings = full_refresh._validate_result(
            self.artist, fake_chat_response(result), executions(),
            url_checker=lambda url: True,
        )

        self.assertEqual([], value["events"])
        self.assertEqual([], value["rumors"])
        self.assertEqual(1, len(sources))
        self.assertEqual(2, len(warnings))

    def test_unreachable_source_and_candidate_are_discarded(self):
        result = complete_result(events=[event()], sources=[source()])
        value, sources, warnings = full_refresh._validate_result(
            self.artist, fake_chat_response(result), executions(),
            url_checker=lambda url: False,
        )

        self.assertEqual([], sources)
        self.assertEqual([], value["events"])
        self.assertEqual(2, len(warnings))

    def test_private_source_url_is_rejected_before_url_check(self):
        private = "http://127.0.0.1/admin"
        checker = mock.Mock(return_value=True)
        result = complete_result(events=[event(private)], sources=[source(private)])
        value, sources, warnings = full_refresh._validate_result(
            self.artist, fake_chat_response(result), executions(), checker,
        )

        checker.assert_not_called()
        self.assertEqual([], sources)
        self.assertEqual([], value["events"])
        self.assertEqual(2, len(warnings))

    def test_incomplete_coverage_fails(self):
        result = complete_result()
        result["coverage"]["rumors_checked"] = False
        with self.assertRaises(full_refresh.ResearchError):
            full_refresh._validate_result(
                self.artist, fake_chat_response(result), executions(),
                url_checker=lambda url: True,
            )

    def test_failed_formula_fails_closed(self):
        requester = mock.Mock(return_value=fake_fiber(status="failed"))
        with mock.patch.object(full_refresh.time, "sleep"):
            with self.assertRaises(full_refresh.ResearchError):
                full_refresh.execute_searches(
                    self.artist, "2026-08-12", requester=requester,
                )
        self.assertEqual(full_refresh.MAX_RETRIES, requester.call_count)

    def test_local_schema_validation_rejects_extra_fields(self):
        result = complete_result()
        result["unexpected"] = True
        with self.assertRaises(full_refresh.ResearchError):
            full_refresh._validate_result(
                self.artist, fake_chat_response(result), executions(),
                url_checker=lambda url: True,
            )

    def test_research_archive_records_category_query_without_protected_output(self):
        result = complete_result(events=[event()], sources=[source()])
        payload = full_refresh.research_all(
            [self.artist], "kimi-k3", workers=1,
            requester=lambda body: fake_chat_response(result),
            search_requester=mock.Mock(side_effect=[fake_fiber(i) for i in range(4)]),
            tools=TOOLS,
            url_checker=lambda url: True,
        )

        self.assertEqual("kimi-k3-formula-web-search", payload["_meta"]["by"])
        queries = payload["_meta"]["queries"]["mock"]
        self.assertEqual(list(full_refresh.SEARCH_CATEGORIES), [
            item["category"] for item in queries
        ])
        self.assertTrue(all("query" in item for item in queries))
        self.assertTrue(all("output" not in item for item in queries))
        self.assertEqual("ticketing", payload["sources"][0]["category"])

    def test_source_reachability_is_limited_to_first_40_referenced_urls(self):
        urls = ["https://tickets.example.com/show/%d" % index for index in range(45)]
        result = complete_result(
            events=[event(url) for url in urls],
            sources=[source(url) for url in urls],
        )
        checker = mock.Mock(return_value=True)

        value, sources, warnings = full_refresh._validate_result(
            self.artist, fake_chat_response(result), executions(), checker,
        )

        self.assertEqual(40, checker.call_count)
        self.assertEqual(40, len(sources))
        self.assertEqual(40, len(value["events"]))
        self.assertTrue(any("超过 40" in warning for warning in warnings))

    def test_retry_delay_is_exponential_and_honors_retry_after(self):
        self.assertEqual(5.0, full_refresh._retry_delay(0))
        self.assertEqual(20.0, full_refresh._retry_delay(2))
        self.assertEqual(45.0, full_refresh._retry_delay(0, {"Retry-After": "45"}))

    def test_tier_zero_defaults_to_serial_and_quota_errors_are_not_retryable(self):
        self.assertEqual(1, full_refresh.DEFAULT_WORKERS)
        self.assertTrue(full_refresh._quota_exhausted(
            '{"error":{"code":"insufficient_quota"}}'
        ))
        self.assertTrue(full_refresh._quota_exhausted(
            '{"error":{"code":"exceeded_current_quota_error"}}'
        ))
        self.assertFalse(full_refresh._quota_exhausted(
            '{"error":{"code":"rate_limit_reached"}}'
        ))

    def test_quota_error_bypasses_outer_retries(self):
        search_requester = mock.Mock(side_effect=full_refresh.QuotaError("余额不足"))
        with self.assertRaises(full_refresh.QuotaError):
            full_refresh.execute_searches(
                self.artist, "2026-08-12", requester=search_requester,
            )
        search_requester.assert_called_once()

    def test_quota_error_stops_remaining_artists(self):
        search_requester = mock.Mock(side_effect=full_refresh.QuotaError("余额不足"))
        other = {**self.artist, "key": "other", "name": "Other Artist"}
        with self.assertRaises(full_refresh.QuotaError):
            full_refresh.research_all(
                [self.artist, other], "kimi-k3", workers=1,
                search_requester=search_requester, tools=TOOLS,
                url_checker=lambda url: True,
            )
        search_requester.assert_called_once()

    def test_empty_rumor_date_is_discarded(self):
        rumor = {
            "headline": "Missing date", "detail": "", "source_name": "forum",
            "url": "https://tickets.example.com/show/1", "credibility": "low",
            "posted_at": "",
        }
        value, _, warnings = full_refresh._validate_result(
            self.artist,
            fake_chat_response(complete_result(rumors=[rumor], sources=[source()])),
            executions(), url_checker=lambda url: True,
        )
        self.assertEqual([], value["rumors"])
        self.assertTrue(any("posted_at" in warning for warning in warnings))

    def test_site_data_exposes_full_refresh_identity(self):
        self.assertIn("full_refresh_id", full_refresh.monitor.build_site.__code__.co_consts)

    def test_incomplete_showstart_coverage_blocks_publish(self):
        config = {"artists": [
            {"key": "cn", "name": "CN", "region": "cn", "enabled": True},
            {"key": "kp", "name": "KP", "region": "kpop", "enabled": True},
        ]}
        with mock.patch.object(full_refresh.monitor, "load_config", return_value=config):
            with self.assertRaises(full_refresh.ResearchError):
                full_refresh.validate_showstart_coverage({
                    "source_status": {"showstart": {"ok": 0, "fail": 1}},
                })
            full_refresh.validate_showstart_coverage({
                "source_status": {"showstart": {"ok": 1, "fail": 0}},
            })

    def test_reconciliation_distinguishes_seen_and_unverified_records(self):
        events = {
            "seen": {
                "artist_key": "mock", "show_date": "2099-12-01",
                "last_seen_run": "run-1",
            },
            "missed": {
                "artist_key": "mock", "show_date": "2099-12-02",
                "last_seen_run": "older",
            },
        }
        rumors = {
            "seen-rumor": {"artist_key": "mock", "last_seen_run": "run-1"},
        }
        saved = {}
        with mock.patch.object(
            full_refresh.store, "_load", side_effect=[events, rumors],
        ), mock.patch.object(
            full_refresh.store, "_save", side_effect=lambda path, value: saved.__setitem__(path, value),
        ):
            summary = full_refresh.store.reconcile_full_refresh(
                "run-1", "0123456789abcdef01234567", ["mock"], "2099-01-01T00:00:00",
            )
        self.assertEqual("verified", events["seen"]["verification_status"])
        self.assertEqual("unverified", events["missed"]["verification_status"])
        self.assertEqual(1, events["missed"]["missed_full_refreshes"])
        self.assertEqual("verified", rumors["seen-rumor"]["verification_status"])
        self.assertEqual(1, summary["events_unverified"])
        self.assertEqual(2, len(saved))


if __name__ == "__main__":
    unittest.main()
