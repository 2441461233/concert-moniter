import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
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


def fake_fiber(
    index=0, status="succeeded", url="https://tickets.example.com/show/1",
):
    return {
        "id": "fiber-%d" % index,
        "object": "fiber",
        "status": status,
        "context": {
            "encrypted_output": (
                "----MOONSHOT ENCRYPTED BEGIN----result-%d"
                "----MOONSHOT ENCRYPTED END----" % index
            ),
            "references": [{"title": "Provider result", "url": url}],
        },
        "formula": "moonshot/web-search:latest",
    }


def fake_chat_response(result, finish_reason="stop", usage=None):
    response = {
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
    if usage is not None:
        response["usage"] = usage
    return response


def complete_result(events=None, rumors=None, sources=None):
    return {
        "events": [] if events is None else events,
        "rumors": [] if rumors is None else rumors,
        "coverage": {
            "ticketing_checked": True,
            "official_checked": True,
            "china_region_checked": True,
            "rumors_checked": True,
            "summary": "All four code-executed search categories were read.",
        },
    }


def source(url="https://tickets.example.com/show/1", category="ticketing"):
    return {
        "source_id": full_refresh._source_id_for_url(url),
        "category": category,
        "title": "Official ticket page",
        "url": url,
    }


def event(url="https://tickets.example.com/show/1"):
    return {
        "source_id": full_refresh._source_id_for_url(url),
        "tour_name": "Mock Tour",
        "title": "Mock Artist · Shanghai",
        "city": "Shanghai",
        "country": "China",
        "venue": "Mock Arena",
        "show_date": "2026-12-01",
        "doors_time": "18:30",
        "show_time": "19:30",
        "show_end_time": "22:00",
        "curfew_time": "23:00",
        "price": "CNY 380-1280",
        "ticket_tiers": ["CNY 380"],
        "sale_status": "upcoming",
        "sale_time": "2026-10-01 12:00",
        "confidence": "confirmed",
        "note": "Official sale page.",
    }


def executions(url="https://tickets.example.com/show/1"):
    return [{
        "category": category,
        "query": "query for " + category,
        "tool_call_id": "web_search:%d" % index,
        "fiber_id": "fiber-%d" % index,
        "output": "encrypted-result-%d" % index,
        "source_catalog": [source(url, category)],
    } for index, category in enumerate(full_refresh.SEARCH_CATEGORIES)]


def validate_v2(artist, response, searches, url_checker=lambda url: True):
    return full_refresh._validate_result(
        artist, response, searches, url_checker,
        contract="source_id_v2",
    )


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
        tool_messages = request["messages"][3:-1]
        catalog_message = request["messages"][-1]
        self.assertEqual(4, len(assistant["tool_calls"]))
        self.assertEqual(4, len(tool_messages))
        self.assertEqual(
            [call["id"] for call in assistant["tool_calls"]],
            [message["tool_call_id"] for message in tool_messages],
        )
        self.assertTrue(all(message["role"] == "tool" for message in tool_messages))
        self.assertEqual("user", catalog_message["role"])
        self.assertIn("PROGRAM_SOURCE_CATALOG", catalog_message["content"])
        event_schema = full_refresh.ENRICH_RESULT_SCHEMA["properties"]["events"]["items"]
        self.assertIn("source_id", event_schema["required"])
        self.assertNotIn("url", event_schema["properties"])
        self.assertIn("doors_time", event_schema["required"])
        self.assertIn("show_end_time", event_schema["required"])
        self.assertIn("curfew_time", event_schema["required"])

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

    def test_unknown_model_source_id_fails_closed(self):
        bad_rumor = {
            "headline": "Vague date", "detail": "", "source_name": "forum",
            "source_id": source()["source_id"], "credibility": "low",
            "posted_at": "2026-08",
        }
        result = complete_result(
            events=[event("https://invented.example/event")],
            rumors=[bad_rumor],
            sources=[source()],
        )
        with self.assertRaisesRegex(full_refresh.ResearchError, "unknown program source_id"):
            validate_v2(
                self.artist, fake_chat_response(result), executions(),
                url_checker=lambda url: True,
            )

    def test_unreachable_source_fails_entire_candidate(self):
        result = complete_result(events=[event()], sources=[source()])
        with self.assertRaisesRegex(full_refresh.ResearchError, "unsafe or invalid"):
            validate_v2(
                self.artist, fake_chat_response(result), executions(),
                url_checker=lambda url: False,
            )

    def test_private_source_url_is_rejected_before_url_check(self):
        private = "http://127.0.0.1/admin"
        checker = mock.Mock(return_value=True)
        result = complete_result(events=[event(private)])
        with self.assertRaisesRegex(full_refresh.ResearchError, "catalog"):
            validate_v2(
                self.artist, fake_chat_response(result), executions(private), checker,
            )
        checker.assert_not_called()

    def test_confirmed_event_cannot_use_only_rumor_search_source(self):
        rumor_url = "https://forum.example.com/thread/1"
        raw = event(rumor_url)
        searches = executions(rumor_url)
        for search in searches[:3]:
            search["source_catalog"] = []
        searches[3]["source_catalog"] = [source(rumor_url, "rumors")]
        with self.assertRaisesRegex(full_refresh.ResearchError, "rumors"):
            validate_v2(
                self.artist,
                fake_chat_response(complete_result(events=[raw])),
                searches, url_checker=lambda url: True,
            )

    def test_incomplete_coverage_fails(self):
        result = complete_result()
        result["coverage"]["rumors_checked"] = False
        with self.assertRaises(full_refresh.ResearchError):
            validate_v2(
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
            validate_v2(
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

        self.assertEqual(
            "moonshot-formula-web-search-candidate", payload["_meta"]["by"],
        )
        queries = payload["_meta"]["queries"]["mock"]
        self.assertEqual(list(full_refresh.SEARCH_CATEGORIES), [
            item["category"] for item in queries
        ])
        self.assertTrue(all("query" in item for item in queries))
        self.assertTrue(all("output" not in item for item in queries))
        self.assertEqual("ticketing", payload["sources"][0]["category"])

    def test_telemetry_counts_paid_validation_retries_before_validation(self):
        incomplete = complete_result()
        incomplete["coverage"]["official_checked"] = False
        first = fake_chat_response(incomplete, usage={
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "cached_tokens": 100,
        })
        second = fake_chat_response(complete_result(), usage={
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "total_tokens": 2300,
            "cached_tokens": 500,
        })
        telemetry = full_refresh.RefreshTelemetry("kimi-k3")
        with mock.patch.object(full_refresh.time, "sleep"):
            full_refresh.research_artist(
                self.artist, "kimi-k3", "2026-08-12", TOOLS,
                requester=mock.Mock(side_effect=[first, second]),
                search_requester=mock.Mock(side_effect=[fake_fiber(i) for i in range(4)]),
                url_checker=lambda url: True,
                retries=2,
                telemetry=telemetry,
            )

        snapshot = telemetry.snapshot(status="completed")
        summary = snapshot["summary"]
        self.assertEqual(4, summary["formula_calls"])
        self.assertEqual(2, summary["chat_calls"])
        self.assertEqual(3000, summary["prompt_tokens"])
        self.assertEqual(600, summary["cached_tokens"])
        self.assertEqual(500, summary["completion_tokens_including_reasoning"])
        self.assertAlmostEqual(0.0992, summary["chat_estimated_cost_cny"])
        self.assertAlmostEqual(0.2192, summary["total_estimated_cost_cny"])
        self.assertTrue(snapshot["chat_calls"][0]["validation"].startswith("failed:"))
        self.assertEqual("passed", snapshot["chat_calls"][1]["validation"])

    def test_telemetry_counts_formula_retry_and_never_serializes_content(self):
        telemetry = full_refresh.RefreshTelemetry("kimi-k3")
        requester = mock.Mock(side_effect=[
            fake_fiber(status="failed"),
            fake_fiber(0), fake_fiber(1), fake_fiber(2), fake_fiber(3),
        ])
        with mock.patch.object(full_refresh.time, "sleep"):
            full_refresh.execute_searches(
                self.artist, "2026-08-12", requester=requester,
                telemetry=telemetry,
            )
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "usage.json"
            telemetry.write(path, status="completed")
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertEqual(5, payload["summary"]["formula_calls"])
        self.assertEqual(4, payload["summary"]["formula_succeeded"])
        self.assertNotIn("MOONSHOT ENCRYPTED", raw)
        self.assertNotIn("result-0", raw)

    def test_malformed_paid_responses_are_counted_before_failing_closed(self):
        formula_telemetry = full_refresh.RefreshTelemetry("kimi-k3")
        with self.assertRaisesRegex(full_refresh.ResearchError, "顶层"):
            full_refresh.execute_searches(
                self.artist, "2026-08-12", requester=lambda body: ["bad-root"],
                telemetry=formula_telemetry, max_attempts=1,
            )
        formula_snapshot = formula_telemetry.snapshot(status="failed")
        self.assertEqual(1, formula_snapshot["summary"]["formula_calls"])
        self.assertEqual("failed", formula_snapshot["formula_calls"][0]["outcome"])

        chat_telemetry = full_refresh.RefreshTelemetry("kimi-k3")
        with self.assertRaises(full_refresh.ResearchError):
            full_refresh.research_artist(
                self.artist, "kimi-k3", "2026-08-12", TOOLS,
                requester=lambda body: ["bad-root"],
                search_requester=mock.Mock(
                    side_effect=[fake_fiber(i) for i in range(4)]
                ),
                url_checker=lambda url: True,
                retries=1, search_retries=1, telemetry=chat_telemetry,
            )
        chat_snapshot = chat_telemetry.snapshot(status="failed")
        self.assertEqual(1, chat_snapshot["summary"]["chat_calls"])
        self.assertEqual(0, chat_snapshot["summary"]["chat_usage_reported_calls"])
        self.assertIsNone(chat_snapshot["summary"]["chat_estimated_cost_cny"])
        self.assertTrue(
            chat_snapshot["chat_calls"][0]["validation"].startswith("failed:")
        )

    def test_research_payload_embeds_sanitized_billing_summary(self):
        telemetry = full_refresh.RefreshTelemetry("kimi-k3")
        result = complete_result(events=[event()], sources=[source()])
        response = fake_chat_response(result, usage={
            "prompt_tokens": 2500,
            "completion_tokens": 400,
            "total_tokens": 2900,
            "cached_tokens": 250,
        })
        payload = full_refresh.research_all(
            [self.artist], "kimi-k3", workers=1,
            requester=lambda body: response,
            search_requester=mock.Mock(side_effect=[fake_fiber(i) for i in range(4)]),
            tools=TOOLS,
            url_checker=lambda url: True,
            telemetry=telemetry,
        )

        self.assertEqual(1, payload["_meta"]["billing"]["chat_calls"])
        self.assertEqual(4, payload["_meta"]["billing_by_artist"]["mock"]["formula_calls"])
        self.assertNotIn("formula_calls", payload["_meta"]["queries"]["mock"][0])

    def test_telemetry_output_must_be_outside_repository(self):
        with self.assertRaises(full_refresh.ResearchError):
            full_refresh.resolve_telemetry_output(
                full_refresh.ROOT / "data" / "refresh-telemetry.json"
            )
        with tempfile.TemporaryDirectory() as tempdir:
            external = Path(tempdir) / "refresh-telemetry.json"
            self.assertEqual(
                external.resolve(), full_refresh.resolve_telemetry_output(external),
            )

    def test_telemetry_does_not_apply_china_prices_to_other_api_regions(self):
        telemetry = full_refresh.RefreshTelemetry(
            "kimi-k3", context={"pricing_region": "unknown"},
        )
        telemetry.record_chat(
            artist_key="mock", attempt=1, request_payload={},
            response=fake_chat_response(complete_result(), usage={
                "prompt_tokens": 1000, "completion_tokens": 100,
            }),
            elapsed_seconds=1.0, outcome="response_received",
        )
        summary = telemetry.snapshot(status="completed")["summary"]
        self.assertIsNone(summary["chat_estimated_cost_cny"])
        self.assertIsNone(summary["formula_estimated_cost_cny"])
        self.assertIsNone(summary["total_estimated_cost_cny"])

    def test_more_than_40_referenced_source_ids_fails_before_url_checks(self):
        urls = ["https://tickets.example.com/show/%d" % index for index in range(45)]
        result = complete_result(
            events=[event(url) for url in urls],
            sources=[source(url) for url in urls],
        )
        checker = mock.Mock(return_value=True)
        searches = executions()
        searches[0]["source_catalog"] = [source(url) for url in urls]
        with self.assertRaisesRegex(full_refresh.ResearchError, "more than 40"):
            validate_v2(
                self.artist, fake_chat_response(result), searches, checker,
            )
        checker.assert_not_called()

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

    def test_empty_rumor_date_fails_entire_candidate(self):
        rumor = {
            "headline": "Missing date", "detail": "", "source_name": "forum",
            "source_id": source()["source_id"], "credibility": "low",
            "posted_at": "",
        }
        with self.assertRaisesRegex(full_refresh.ResearchError, "posted_at"):
            validate_v2(
                self.artist,
                fake_chat_response(complete_result(rumors=[rumor], sources=[source()])),
                executions(), url_checker=lambda url: True,
            )

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

    def test_model_cannot_output_even_a_correct_url(self):
        raw = event()
        raw["url"] = "https://tickets.example.com/show/1"
        with self.assertRaisesRegex(full_refresh.ResearchError, "多余字段: url"):
            validate_v2(
                self.artist,
                fake_chat_response(complete_result(events=[raw])),
                executions(), url_checker=lambda url: True,
            )

    def test_formula_without_enumerable_references_blocks_chat(self):
        fiber = fake_fiber()
        del fiber["context"]["references"]
        searches = full_refresh.execute_searches(
            self.artist, "2026-08-12",
            requester=mock.Mock(side_effect=[fiber, fiber, fiber, fiber]),
            max_attempts=1,
        )
        with self.assertRaisesRegex(full_refresh.ResearchError, "reference catalog"):
            full_refresh.build_request(
                self.artist, "kimi-k3", "2026-08-12", TOOLS, searches,
            )

    def test_program_backfills_original_provider_url(self):
        provider_url = "https://tickets.example.com/original?a=1"
        result = complete_result(events=[event(provider_url)])
        value, sources, warnings = validate_v2(
            self.artist, fake_chat_response(result), executions(provider_url),
            url_checker=lambda url: True,
        )
        self.assertEqual(provider_url, value["events"][0]["url"])
        self.assertEqual(provider_url, sources[0]["url"])
        self.assertEqual([], warnings)

    def test_time_roles_are_distinct_and_ambiguous_format_is_rejected(self):
        raw = event()
        raw["show_time"] = "doors 18:30"
        with self.assertRaisesRegex(full_refresh.ResearchError, "show_time"):
            validate_v2(
                self.artist, fake_chat_response(complete_result(events=[raw])),
                executions(), url_checker=lambda url: True,
            )
        prompt = full_refresh.build_prompt(self.artist, "2026-08-12")
        self.assertIn("不得把 doors/curfew", prompt)
        self.assertIn("show_time/show_end_time", prompt)

    def test_store_persists_each_time_role_without_cross_filling(self):
        normalized = full_refresh.store.normalize_event({
            **event(),
            "artist_key": "mock",
            "artist_name": "Mock Artist",
            "source": "research",
        })
        self.assertEqual("18:30", normalized["doors_time"])
        self.assertEqual("19:30", normalized["show_time"])
        self.assertEqual("22:00", normalized["show_end_time"])
        self.assertEqual("23:00", normalized["curfew_time"])

        merged = full_refresh.store._merge_one(
            normalized,
            {
                **normalized,
                "doors_time": "",
                "show_time": "20:00",
                "show_end_time": "",
                "curfew_time": "23:30",
            },
        )
        self.assertEqual("18:30", merged["doors_time"])
        self.assertEqual("20:00", merged["show_time"])
        self.assertEqual("22:00", merged["show_end_time"])
        self.assertEqual("23:30", merged["curfew_time"])

    def test_research_citation_id_does_not_collapse_multiple_events(self):
        citation_url = "https://artist.example.com/official-tour"
        first = {
            **event(citation_url),
            "source": "research", "artist_key": "mock",
            "artist_name": "Mock Artist", "show_date": "2099-12-01",
            "city": "Shanghai",
        }
        second = {
            **first, "show_date": "2099-12-02", "city": "Beijing",
        }
        saved = {}
        with mock.patch.object(
            full_refresh.store, "_load", return_value={},
        ), mock.patch.object(
            full_refresh.store, "_save",
            side_effect=lambda path, value: saved.update(value),
        ):
            full_refresh.store.merge_events([first, second], "run-1")
        self.assertEqual(2, len(saved))
        for record in saved.values():
            evidence = record["sources"][0]
            self.assertEqual("", evidence["source_id"])
            self.assertEqual(first["source_id"], evidence["citation_id"])

    def test_same_day_same_city_distinct_showstart_ids_remain_two_events(self):
        first = {
            **event(), "source": "showstart", "source_id": "event-early",
            "artist_key": "mock", "artist_name": "Mock Artist",
            "show_date": "2099-12-01", "city": "Shanghai",
            "show_time": "14:00", "title": "Matinee",
        }
        second = {
            **first, "source_id": "event-evening", "show_time": "19:30",
            "title": "Evening",
        }
        saved = {}
        with mock.patch.object(
            full_refresh.store, "_load", return_value={},
        ), mock.patch.object(
            full_refresh.store, "_save",
            side_effect=lambda path, value: saved.update(value),
        ):
            full_refresh.store.merge_events([first, second], "run-1")
        self.assertEqual(2, len(saved))
        self.assertEqual({"14:00", "19:30"}, {
            item["show_time"] for item in saved.values()
        })

    def test_research_only_fills_empty_deterministic_fields(self):
        showstart = full_refresh.store.normalize_event({
            **event(),
            "source": "showstart", "source_id": "12345",
            "artist_key": "mock", "artist_name": "Mock Artist",
            "title": "Collector title", "city": "Shanghai",
            "venue": "Collector Arena", "show_time": "19:30",
            "price": "CNY 680", "sale_status": "on_sale",
            "sale_time": "",
        })
        research = full_refresh.store.normalize_event({
            **event(),
            "source": "research", "artist_key": "mock",
            "artist_name": "Mock Artist", "title": "Wrong model title",
            "city": "Wrong city", "venue": "Wrong venue",
            "show_time": "20:30", "price": "CNY 1",
            "sale_status": "cancelled", "sale_time": "2099-01-01 12:00",
        })
        merged = full_refresh.store._merge_one(showstart, research)
        self.assertEqual("showstart", merged["source"])
        self.assertEqual("Collector title", merged["title"])
        self.assertEqual("Shanghai", merged["city"])
        self.assertEqual("Collector Arena", merged["venue"])
        self.assertEqual("19:30", merged["show_time"])
        self.assertEqual("CNY 680", merged["price"])
        self.assertEqual("on_sale", merged["sale_status"])
        self.assertEqual("2099-01-01 12:00", merged["sale_time"])

        corrected = full_refresh.store._merge_one(research, showstart)
        self.assertEqual("showstart", corrected["source"])
        self.assertEqual("Collector title", corrected["title"])
        self.assertEqual("Collector Arena", corrected["venue"])

    def test_showstart_normalization_does_not_duplicate_legacy_source_shape(self):
        old = full_refresh.store.normalize_event({
            **event(), "source": "showstart", "source_id": "12345",
            "artist_key": "mock", "artist_name": "Mock Artist",
        })
        self.assertNotIn("citation_id", old["sources"][0])
        incoming = full_refresh.store.normalize_event({
            **event(), "source": "showstart", "source_id": "12345",
            "artist_key": "mock", "artist_name": "Mock Artist",
        })
        merged = full_refresh.store._merge_one(old, incoming)
        self.assertEqual(1, len(merged["sources"]))

    def test_context_ceiling_stops_chat_after_search(self):
        large = fake_fiber()
        large["context"]["encrypted_output"] = "中" * 20_000
        chat = mock.Mock()
        with self.assertRaises(full_refresh.EnrichmentContextError):
            full_refresh.research_artist(
                self.artist, "kimi-k3", "2026-08-12", TOOLS,
                requester=chat,
                search_requester=mock.Mock(side_effect=[large] * 4),
                url_checker=lambda url: True,
                retries=1, search_retries=1, max_context_bytes=2_000,
            )
        chat.assert_not_called()

    def test_global_budget_blocks_all_artists_before_paid_calls(self):
        with self.assertRaisesRegex(
            full_refresh.EnrichmentBudgetError, "global limit",
        ):
            full_refresh.preflight_enrichment_budget(
                artist_count=12, model="kimi-k3",
                global_limit_cny=5.0, per_artist_limit_cny=5.0,
                max_context_bytes=128_000, attempts=1,
            )

    def test_stale_price_table_blocks_before_any_paid_enrichment(self):
        with self.assertRaisesRegex(
            full_refresh.EnrichmentBudgetError, "freshness window",
        ):
            full_refresh.preflight_enrichment_budget(
                artist_count=1, model="kimi-k3",
                global_limit_cny=10.0, per_artist_limit_cny=10.0,
                max_context_bytes=2_000, attempts=1,
                pricing_today="2026-09-24",
            )

    def test_paid_endpoint_rejects_non_china_route(self):
        with mock.patch.dict(
            os.environ, {"MOONSHOT_API_BASE": "https://api.moonshot.ai/v1"},
            clear=False,
        ):
            with self.assertRaises(full_refresh.EnrichmentBudgetError):
                full_refresh.validate_paid_enrichment_endpoint()

    def test_http_200_showstart_challenge_is_not_treated_as_source_success(self):
        normal = (
            '<html><head><title>秀动网（showstart.com）</title>'
            '<script src="https://ssl.captcha.qq.com/TCaptcha.js"></script>'
            '</head><body><div id="__nuxt" data-v-test>'
            '正常的零结果服务端渲染页面' * 8
            + '</div></body></html>'
        )
        challenge = (
            '<html><head><title>访问验证 - 秀动 ShowStart</title></head>'
            '<body><div id="captcha-container">请输入验证码</div>'
            + ('异常响应' * 20) + '</body></html>'
        )
        with mock.patch.object(
            full_refresh.monitor.showstart.http, "get", return_value=(normal, None),
        ), mock.patch.object(full_refresh.monitor.showstart.time, "sleep"):
            events, notes, discovered = full_refresh.monitor.showstart.collect(
                self.artist, fetch_details=False, sleep=0,
            )
        self.assertEqual(([], [], None), (events, notes, discovered))

        with mock.patch.object(
            full_refresh.monitor.showstart.http, "get", return_value=(challenge, None),
        ), mock.patch.object(full_refresh.monitor.showstart.time, "sleep"):
            _, notes, _ = full_refresh.monitor.showstart.collect(
                self.artist, fetch_details=False, sleep=0,
            )
        self.assertTrue(notes)
        self.assertTrue(all("挑战页" in item for item in notes))

    def test_showstart_detail_200_challenge_returns_error(self):
        challenge = (
            '<html><head><title>安全验证 - ShowStart 秀动</title></head>'
            '<body><div class="verify-human">verify you are human</div>'
            + ('blocked' * 30) + '</body></html>'
        )
        with mock.patch.object(
            full_refresh.monitor.showstart.http, "get", return_value=(challenge, None),
        ):
            detail, error = full_refresh.monitor.showstart.fetch_detail("123")
        self.assertEqual({}, detail)
        self.assertIn("挑战页", error)

    def test_showstart_event_card_date_parser_drift_marks_source_failed(self):
        drift = (
            '<html><head><title>秀动 ShowStart</title></head>'
            '<body><div id="__nuxt" data-v-test>'
            '<a href="/event/123"><div class="title">Mock Artist Live</div>'
            '<div class="time">Coming soon</div></a>'
            + ('normal shell' * 20) + '</div></body></html>'
        )
        with mock.patch.object(
            full_refresh.monitor.showstart.http, "get", return_value=(drift, None),
        ), mock.patch.object(full_refresh.monitor.showstart.time, "sleep"):
            events, notes, _ = full_refresh.monitor.showstart.collect(
                self.artist, fetch_details=False, sleep=0,
            )
        self.assertEqual([], events)
        self.assertTrue(any("日期" in item for item in notes))

    def test_showstart_event_card_invalid_clock_marks_source_failed(self):
        drift = (
            '<html><head><title>秀动 ShowStart</title></head>'
            '<body><div id="__nuxt" data-v-test>'
            '<a href="/event/123"><div class="title">Mock Artist Live</div>'
            '<div class="time">2026/09/26 99:99</div></a>'
            + ('normal shell' * 20) + '</div></body></html>'
        )
        with mock.patch.object(
            full_refresh.monitor.showstart.http, "get", return_value=(drift, None),
        ), mock.patch.object(full_refresh.monitor.showstart.time, "sleep"):
            events, notes, _ = full_refresh.monitor.showstart.collect(
                self.artist, fetch_details=False, sleep=0,
            )
        self.assertEqual([], events)
        self.assertTrue(any("时间" in item for item in notes))

    def test_candidate_validation_never_fetches_provider_urls(self):
        with mock.patch.object(
            full_refresh.urllib.request, "urlopen",
            side_effect=AssertionError("candidate validator must not fetch"),
        ) as urlopen:
            value, _, _ = full_refresh._validate_result(
                self.artist,
                fake_chat_response(complete_result(events=[event()])),
                executions(), contract="source_id_v2",
            )
        self.assertEqual(1, len(value["events"]))
        urlopen.assert_not_called()

    def test_provider_error_never_echoes_api_key_or_bearer_token(self):
        secret = "sk-sensitive-test-value"
        body = json.dumps({
            "error": {
                "code": "bad_request", "message": (
                    "echo %s Authorization: Bearer bearer-sensitive" % secret
                ),
            },
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.moonshot.cn/v1/chat/completions", 400,
            "Bad Request", {}, io.BytesIO(body),
        )
        with mock.patch.dict(
            os.environ, {"MOONSHOT_API_KEY": secret, "MOONSHOT_REQUEST_INTERVAL": "0"},
            clear=True,
        ), mock.patch.object(full_refresh.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(full_refresh.ResearchError) as caught:
                full_refresh._moonshot_request(
                    "POST", "/chat/completions", {}, max_attempts=1,
                )
        rendered = full_refresh._safe_error(caught.exception)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("bearer-sensitive", rendered)
        self.assertIn("bad_request", rendered)

    def test_deterministic_command_is_strict_and_never_ingests_inbox(self):
        meta = {"source_status": {"showstart": {"ok": 1, "fail": 0}}}
        with mock.patch.object(full_refresh.subprocess, "run") as runner, \
             mock.patch.object(full_refresh.store, "load_meta", return_value=meta), \
             mock.patch.object(full_refresh, "validate_showstart_coverage") as validate:
            self.assertEqual(
                meta, full_refresh.run_deterministic_pipeline(0.1, 2),
            )
        command = runner.call_args.args[0]
        self.assertIn("--strict-sources", command)
        self.assertIn("--no-inbox", command)
        validate.assert_called_once_with(meta)

    def test_missing_production_store_blocks_before_collector(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.object(
            full_refresh, "ROOT", Path(tempdir),
        ), mock.patch.object(full_refresh.subprocess, "run") as runner:
            with self.assertRaisesRegex(full_refresh.ResearchError, "缺少"):
                full_refresh.run_deterministic_pipeline(0.1, 1)
        runner.assert_not_called()

    def test_existing_corrupt_store_is_never_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "events.json"
            path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(full_refresh.store, "EVENTS_PATH", str(path)), \
                 mock.patch.object(full_refresh.store, "_save") as save:
                with self.assertRaises(full_refresh.store.StoreDataError):
                    full_refresh.store.merge_events([event()], "run-1")
            save.assert_not_called()

    def test_finalize_stale_preserves_old_research_and_never_reconciles(self):
        original = {
            "last_research_at": "2026-08-01T00:00:00",
            "source_status": {"showstart": {"ok": 1, "fail": 0}},
            "notes": [],
        }
        saved = {}
        with mock.patch.object(
            full_refresh.store, "load_meta", return_value=original,
        ), mock.patch.object(
            full_refresh.store, "save_meta",
            side_effect=lambda value: saved.update(value),
        ), mock.patch.object(
            full_refresh.store, "reconcile_full_refresh",
        ) as reconcile, mock.patch.object(full_refresh.monitor, "build_site"):
            result = full_refresh.finalize_refresh_metadata(
                run_id="0123456789abcdef01234567", artists=[self.artist],
                provider="none", enrichment_status="disabled_stale",
                error=full_refresh.ResearchError("disabled"),
            )
        reconcile.assert_not_called()
        self.assertEqual("2026-08-01T00:00:00", result["last_research_at"])
        self.assertEqual(
            "deterministic_completed_enrichment_stale",
            result["full_refresh_status"],
        )
        self.assertTrue(result["source_status"]["research"]["stale"])
        self.assertEqual("no_enrichment_write",
                         result["full_refresh"]["enrichment"]["write_policy"])
        self.assertEqual("", result["full_refresh"]["enrichment"]["model"])
        self.assertEqual(
            "not_requested",
            result["full_refresh"]["enrichment"]["promotion_status"],
        )
        self.assertEqual(result, saved)

    def test_main_default_none_without_key_publishes_deterministic_stale(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ) as deterministic, mock.patch.object(
            full_refresh, "research_all",
        ) as research, mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_stale",
            },
        ) as finalize, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(0, code)
        deterministic.assert_called_once()
        research.assert_not_called()
        self.assertEqual(
            "disabled_stale", finalize.call_args.kwargs["enrichment_status"],
        )

    def test_invalid_provider_from_environment_fails_before_deterministic(self):
        with mock.patch.dict(
            os.environ, {"LLM_ENRICH_PROVIDER": "qwen"}, clear=True,
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline",
        ) as deterministic:
            self.assertEqual(2, full_refresh.main([]))
        deterministic.assert_not_called()

    def test_main_missing_kimi_key_still_publishes_stale(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ), mock.patch.object(
            full_refresh, "research_all",
        ) as research, mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_stale",
            },
        ) as finalize, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--enrich-provider", "kimi",
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(0, code)
        research.assert_not_called()
        self.assertEqual(
            "skipped_stale", finalize.call_args.kwargs["enrichment_status"],
        )

    def test_main_quota_failure_does_not_block_deterministic_publish(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "test-key"}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ), mock.patch.object(
            full_refresh, "validate_paid_enrichment_endpoint",
        ), mock.patch.object(
            full_refresh, "preflight_enrichment_budget", return_value={"reserved": 1},
        ), mock.patch.object(
            full_refresh, "research_all", side_effect=full_refresh.QuotaError("余额不足"),
        ), mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_failed",
            },
        ) as finalize, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--enrich-provider", "kimi",
                "--enrich-max-cost-cny", "100",
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(0, code)
        self.assertEqual(
            "failed_stale", finalize.call_args.kwargs["enrichment_status"],
        )

    def test_main_kimi_success_writes_only_external_candidate(self):
        payload = {
            "_meta": {
                "artists_succeeded": 1, "events_found": 1,
                "rumors_found": 0, "sources_consulted": 1,
                "warnings": [], "model": "kimi-k3",
            },
            "events": [event()], "rumors": [], "sources": [source()],
        }
        events_before = (full_refresh.ROOT / "data" / "events.json").read_bytes()
        rumors_before = (full_refresh.ROOT / "data" / "rumors.json").read_bytes()
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "test-key"}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ), mock.patch.object(
            full_refresh, "validate_paid_enrichment_endpoint",
        ), mock.patch.object(
            full_refresh, "preflight_enrichment_budget", return_value={"reserved": 1},
        ), mock.patch.object(
            full_refresh, "research_all", return_value=payload,
        ) as research, mock.patch.object(
            full_refresh.monitor, "_ingest_one",
        ) as ingest, mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_stale",
            },
        ) as finalize, mock.patch.object(full_refresh, "_git_head", return_value=""):
            candidate = Path(tempdir) / "candidate.json"
            code = full_refresh.main([
                "--enrich-provider", "kimi",
                "--enrich-max-cost-cny", "100",
                "--candidate-output", str(candidate),
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
            saved = json.loads(candidate.read_text(encoding="utf-8"))
        self.assertEqual(0, code)
        research.assert_called_once()
        ingest.assert_not_called()
        self.assertEqual(
            "candidate_ready_stale",
            finalize.call_args.kwargs["enrichment_status"],
        )
        self.assertIs(saved["_meta"]["production_write"], False)
        self.assertEqual(
            "candidate_only_pending_manual_validation",
            saved["_meta"]["promotion_status"],
        )
        self.assertEqual(
            events_before, (full_refresh.ROOT / "data" / "events.json").read_bytes(),
        )
        self.assertEqual(
            rumors_before, (full_refresh.ROOT / "data" / "rumors.json").read_bytes(),
        )

    def test_invalid_candidate_path_blocks_before_any_paid_call(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "test-key"}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ), mock.patch.object(
            full_refresh, "research_all",
        ) as research, mock.patch.object(
            full_refresh, "validate_paid_enrichment_endpoint",
        ) as endpoint, mock.patch.object(
            full_refresh, "preflight_enrichment_budget",
        ) as preflight, mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_failed",
            },
        ) as finalize, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--enrich-provider", "kimi",
                "--candidate-output", str(full_refresh.ROOT / "data" / "bad.json"),
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(0, code)
        research.assert_not_called()
        endpoint.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(
            "failed_stale", finalize.call_args.kwargs["enrichment_status"],
        )

    def test_existing_candidate_path_blocks_before_any_paid_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            candidate = Path(tempdir) / "candidate.json"
            candidate.write_text("do not overwrite", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"MOONSHOT_API_KEY": "test-key"}, clear=True,
            ), mock.patch.object(
                full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
            ), mock.patch.object(
                full_refresh, "run_deterministic_pipeline", return_value={},
            ), mock.patch.object(
                full_refresh, "research_all",
            ) as research, mock.patch.object(
                full_refresh, "finalize_refresh_metadata",
                return_value={
                    "full_refresh_status": "deterministic_completed_enrichment_failed",
                },
            ), mock.patch.object(full_refresh, "_git_head", return_value=""):
                code = full_refresh.main([
                    "--enrich-provider", "kimi",
                    "--candidate-output", str(candidate),
                    "--telemetry-output", str(Path(tempdir) / "usage.json"),
                ])
            self.assertEqual(0, code)
            research.assert_not_called()
            self.assertEqual("do not overwrite", candidate.read_text(encoding="utf-8"))

    def test_candidate_ready_metadata_stays_stale_and_preserves_last_research(self):
        original = {
            "last_research_at": "2026-08-01T00:00:00",
            "source_status": {"showstart": {"ok": 1, "fail": 0}},
            "notes": [],
        }
        payload = {"_meta": {
            "artists_succeeded": 1, "events_found": 2, "rumors_found": 1,
            "sources_consulted": 3, "warnings": [], "model": "kimi-k3",
        }}
        with mock.patch.object(
            full_refresh.store, "load_meta", return_value=original,
        ), mock.patch.object(
            full_refresh.store, "save_meta",
        ), mock.patch.object(full_refresh.monitor, "build_site"):
            result = full_refresh.finalize_refresh_metadata(
                run_id="0123456789abcdef01234567", artists=[self.artist],
                provider="kimi", enrichment_status="candidate_ready_stale",
                payload=payload,
                error=full_refresh.ResearchError(
                    "candidate_only_pending_manual_validation",
                ),
            )
        self.assertEqual("2026-08-01T00:00:00", result["last_research_at"])
        self.assertEqual(0, result["source_status"]["research"]["ok"])
        self.assertTrue(result["source_status"]["research"]["stale"])
        layer = result["full_refresh"]["enrichment"]
        self.assertEqual(0, layer["events_found"])
        self.assertEqual(2, layer["candidate_events_found"])
        self.assertEqual(
            "candidate_only_external_no_production_merge", layer["write_policy"],
        )

    def test_candidate_only_artifact_is_rejected_by_normal_ingest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            candidate = Path(tempdir) / "candidate.json"
            payload = full_refresh.mark_candidate_only({
                "_meta": {}, "events": [event()], "rumors": [],
            })
            candidate.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "candidate-only"):
                full_refresh.monitor._ingest_one(str(candidate), "run-1")

    def test_malformed_optional_env_is_ignored_when_provider_none(self):
        bad = {
            "LLM_ENRICH_PROVIDER": "none",
            "LLM_ENRICH_MAX_COST_CNY": "bad",
            "LLM_ENRICH_MAX_CONTEXT_BYTES": "bad",
            "LLM_ENRICH_ATTEMPTS": "bad",
            "MOONSHOT_API_BASE": "https://[broken",
        }
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, bad, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", return_value={},
        ) as deterministic, mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
            return_value={
                "full_refresh_status": "deterministic_completed_enrichment_stale",
            },
        ), mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(0, code)
        deterministic.assert_called_once()

    def test_workflow_uploads_but_never_stages_candidate(self):
        workflow = (
            full_refresh.ROOT / ".github" / "workflows" / "full-refresh.yml"
        ).read_text(encoding="utf-8")
        expected = "${{ runner.temp }}/full-refresh-candidate.json"
        self.assertIn("REFRESH_ENRICH_CANDIDATE_OUTPUT: " + expected, workflow)
        self.assertIn("path: " + expected, workflow)
        git_add_line = next(
            line for line in workflow.splitlines() if "git add " in line
        )
        self.assertNotIn("research/archive", git_add_line)
        self.assertNotIn("candidate", git_add_line)

    def test_research_only_validates_store_before_paid_calls(self):
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"MOONSHOT_API_KEY": "test-key"}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "validate_production_store_inputs",
            side_effect=full_refresh.ResearchError("corrupt production store"),
        ), mock.patch.object(
            full_refresh, "research_all",
        ) as research, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--research-only", "--enrich-provider", "kimi",
                "--output", str(Path(tempdir) / "candidate.json"),
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(1, code)
        research.assert_not_called()

    def test_deterministic_failure_does_not_finalize_full_refresh_id(self):
        error = subprocess.CalledProcessError(1, ["monitor.py", "check"])
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {}, clear=True,
        ), mock.patch.object(
            full_refresh.monitor, "load_config", return_value={"artists": [self.artist]},
        ), mock.patch.object(
            full_refresh, "run_deterministic_pipeline", side_effect=error,
        ), mock.patch.object(
            full_refresh, "finalize_refresh_metadata",
        ) as finalize, mock.patch.object(
            full_refresh, "research_all",
        ) as research, mock.patch.object(full_refresh, "_git_head", return_value=""):
            code = full_refresh.main([
                "--telemetry-output", str(Path(tempdir) / "usage.json"),
            ])
        self.assertEqual(1, code)
        finalize.assert_not_called()
        research.assert_not_called()


if __name__ == "__main__":
    unittest.main()
