import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import shadow_compare


PUBLIC_URL = "https://www.ticketmaster.com/mock-artist-tickets/artist/123"
TEST_SECRET = "sk-shadow-test-secret-123456789"


def empty_research():
    return {
        "events": [],
        "rumors": [],
        "sources": [],
        "coverage": {
            "ticketing_checked": True,
            "official_checked": True,
            "china_region_checked": True,
            "rumors_checked": True,
            "summary": "Four frozen search categories were reviewed.",
        },
    }


def final_document():
    return {
        "research": empty_research(),
        "daily_report": "本轮未发现有可靠一级来源支持的新增变化。",
        "decision_notes": ["没有把搜索沉默解释为不存在。"],
    }


def verdict_document():
    return {
        "events": [],
        "rumors": [],
        "conflicts": [],
        "summary": "No supported future event was found in the frozen evidence.",
    }


def response(value, input_tokens=100, output_tokens=20):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        },
    }


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, payload, ledger, purpose, search_calls=0):
        self.calls.append({
            "payload": payload,
            "purpose": purpose,
            "search_calls": search_calls,
        })
        if purpose.startswith("search:"):
            value = response("No supported new event in this category.")
            value["search_info"] = {
                "search_results": [{
                    "title": "Mock official ticket page",
                    "url": PUBLIC_URL,
                    "snippet": "No upcoming event was listed in the captured result.",
                }],
            }
        elif purpose == "glm_adjudication":
            value = response(verdict_document())
        else:
            value = response(final_document())
        ledger.record(
            purpose, payload["model"], value, payload, search_calls=search_calls,
        )
        return value

    def responses(self, payload, ledger, purpose, search_calls=0):
        self.calls.append({
            "payload": payload,
            "purpose": purpose,
            "search_calls": search_calls,
        })
        value = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "mock query",
                        "sources": [{
                            "type": "url",
                            "title": "Mock official ticket page",
                            "url": PUBLIC_URL,
                        }],
                    },
                },
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "No supported new event in this category.",
                        "annotations": [],
                    }],
                },
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        ledger.record(
            purpose, payload["model"], value, payload, search_calls=search_calls,
        )
        return value


class SearchOnlyFakeClient(FakeClient):
    def chat(self, payload, ledger, purpose, search_calls=0):
        raise AssertionError("collect mode invoked a finalizer")


class FailingSearchClient(SearchOnlyFakeClient):
    def __init__(self, fail_on_attempt=2):
        super().__init__()
        self.attempts = 0
        self.fail_on_attempt = fail_on_attempt

    def responses(self, payload, ledger, purpose, search_calls=0):
        self.attempts += 1
        if self.attempts == self.fail_on_attempt:
            raise shadow_compare.ShadowError(
                "forced search failure containing %s" % TEST_SECRET
            )
        return super().responses(payload, ledger, purpose, search_calls)


class ShadowCompareTests(unittest.TestCase):
    def setUp(self):
        self.artist = {
            "key": "mock",
            "name": "Mock Artist",
            "region": "kpop",
            "aliases": ["Mock Artist", "목 아티스트"],
            "search_terms": ["Mock Artist concert"],
            "enabled": True,
        }

    def test_prepare_mode_needs_no_key_and_only_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "prepared"
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                shadow_compare, "_artist_by_key", return_value=(self.artist, 1),
            ), mock.patch.object(
                shadow_compare, "load_historical_baseline", return_value=None,
            ):
                code = shadow_compare.main([
                    "prepare", "--artist-key", "mock",
                    "--as-of", "2026-08-20", "--output-dir", str(output),
                ])

            self.assertEqual(0, code)
            self.assertEqual(["manifest.json"], sorted(path.name for path in output.iterdir()))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("prepared", manifest["status"])
            self.assertEqual(
                "qwen3.7-flash-2026-07-15", manifest["models"]["search"],
            )
            self.assertEqual(4, len(manifest["queries"]))
            self.assertNotIn("api_key", json.dumps(manifest).lower())

    def test_run_mode_fails_closed_without_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "blocked"
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                shadow_compare, "_artist_by_key", return_value=(self.artist, 1),
            ), mock.patch.object(
                shadow_compare, "load_historical_baseline", return_value=None,
            ):
                code = shadow_compare.main([
                    "run", "--artist-key", "mock",
                    "--as-of", "2026-08-20", "--output-dir", str(output),
                ])
            self.assertEqual(2, code)
            self.assertTrue(output.is_dir())
            self.assertEqual([], list(output.iterdir()))

    def test_collect_mode_fails_closed_without_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "blocked-collect"
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                shadow_compare, "_artist_by_key", return_value=(self.artist, 1),
            ):
                code = shadow_compare.main([
                    "collect", "--artist-key", "mock",
                    "--as-of", "2026-08-20", "--output-dir", str(output),
                ])
            self.assertEqual(2, code)
            self.assertTrue(output.is_dir())
            self.assertEqual([], list(output.iterdir()))

    def test_output_path_rejects_repository_and_symlink_escape(self):
        with self.assertRaises(shadow_compare.ShadowError):
            shadow_compare.ensure_external_output_path(
                shadow_compare.ROOT / "research" / "inbox" / "shadow",
            )
        with tempfile.TemporaryDirectory() as tempdir:
            link = Path(tempdir) / "repo-link"
            link.symlink_to(shadow_compare.ROOT, target_is_directory=True)
            with self.assertRaises(shadow_compare.ShadowError):
                shadow_compare.ensure_external_output_path(link / "data" / "shadow")

    def test_run_shadow_rechecks_output_boundary_for_library_callers(self):
        with self.assertRaises(shadow_compare.ShadowError):
            shadow_compare.run_shadow(
                shadow_compare.ROOT / "research" / "inbox",
                self.artist, 1, "2026-08-20", FakeClient(),
                shadow_compare.BudgetLedger(3), baseline=None,
            )

    def test_api_base_allowlist_prevents_key_exfiltration(self):
        self.assertEqual(
            shadow_compare.DEFAULT_API_BASE,
            shadow_compare._safe_api_base(shadow_compare.DEFAULT_API_BASE),
        )
        workspace_base = (
            "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        )
        self.assertEqual(workspace_base, shadow_compare._safe_api_base(workspace_base))
        with self.assertRaises(shadow_compare.ShadowError):
            shadow_compare._safe_api_base(
                "https://attacker.example/compatible-mode/v1",
            )

    def test_source_tier_uses_boundary_safe_explicit_domains(self):
        required_domains = {
            "ticketmaster.com", "ticketmaster.ca", "ticketmaster.com.mx",
            "ticketmaster.co.uk", "ticketmaster.ie", "ticketmaster.nl",
            "ticketmaster.de", "ticketmaster.be", "ticketmaster.dk",
            "livenationentertainment.com", "livenation.com",
            "livenation.asia", "livenation.com.tw", "livenation.hk",
            "livenation.my", "livenation.ph", "weverse.io",
            "katseye.world", "daisychainfields.com", "showstart.com",
            "damai.cn", "piaoxingqiu.com", "maoyan.com", "cityline.com",
            "tixcraft.com", "interpark.com", "nol.com",
        }
        self.assertTrue(required_domains.issubset(
            shadow_compare.PRIMARY_SOURCE_DOMAINS,
        ))
        for domain in required_domains:
            with self.subTest(domain=domain):
                self.assertEqual(
                    "primary", shadow_compare.classify_source_tier(
                        "https://%s/event" % domain,
                    ),
                )
                self.assertEqual(
                    "primary", shadow_compare.classify_source_tier(
                        "https://www.%s/event" % domain,
                    ),
                )

        impostors = (
            "ticketmaster.evil.example",
            "faketicketmaster.com",
            "ticketmaster.com.evil.example",
            "livenation.example",
            "fakelivenation.com",
            "livenation.com.evil.example",
            "katseye.world.evil.example",
            "notdaisychainfields.com",
        )
        for host in impostors:
            with self.subTest(host=host):
                self.assertEqual(
                    "secondary", shadow_compare.classify_source_tier(
                        "https://%s/event" % host,
                    ),
                )

    def test_payloads_bound_cost_and_keep_search_out_of_glm(self):
        query = shadow_compare.full_refresh.build_search_queries(
            self.artist, "2026-08-20",
        )[0]
        search = shadow_compare.build_search_payload(self.artist, query, "2026-08-20")
        self.assertEqual("qwen3.7-flash-2026-07-15", search["model"])
        self.assertEqual([{"type": "web_search"}], search["tools"])
        self.assertGreater(search["max_output_tokens"], 0)

        evidence = self._evidence()
        glm = shadow_compare.build_glm_payload(evidence)
        self.assertEqual("glm-5.2", glm["model"])
        self.assertFalse(glm["enable_thinking"])
        self.assertNotIn("enable_search", glm)
        self.assertGreater(glm["max_tokens"], 0)

        final = shadow_compare.build_final_payload(evidence)
        self.assertEqual("qwen3.8-max", final["model"])
        self.assertTrue(final["response_format"]["json_schema"]["strict"])
        self.assertGreater(final["max_completion_tokens"], 0)

    def test_budget_preflight_stops_before_a_call(self):
        ledger = shadow_compare.BudgetLedger(0.001)
        with self.assertRaises(shadow_compare.BudgetExceeded):
            ledger.preflight(
                "too-expensive", "qwen3.8-max",
                {"model": "qwen3.8-max", "messages": [], "max_tokens": 8000},
            )
        self.assertEqual([], ledger.records)

    def test_usage_and_search_fee_are_accounted(self):
        cost = shadow_compare.calculate_cost_cny(
            "qwen3.7-flash-2026-07-15",
            1_000_000, 1_000_000, search_calls=2,
        )
        self.assertAlmostEqual(6.008, cost)
        ledger = shadow_compare.BudgetLedger(20)
        payload = {
            "model": "qwen3.7-flash-2026-07-15",
            "messages": [], "max_tokens": 100,
        }
        ledger.record(
            "search:ticketing", "qwen3.7-flash-2026-07-15",
            response("ok", 1000, 200), payload, search_calls=1,
        )
        self.assertEqual(1, ledger.records[0]["search_calls"])
        self.assertGreater(ledger.spent_cny, shadow_compare.SEARCH_FEE_CNY)

    def test_qwen_flash_pricing_tiers_include_both_boundaries_without_cache(self):
        model = "qwen3.7-flash-2026-07-15"
        self.assertAlmostEqual(
            0.8064,
            shadow_compare.calculate_cost_cny(model, 32_000, 1_000_000),
        )
        self.assertAlmostEqual(
            2.4192006,
            shadow_compare.calculate_cost_cny(model, 32_001, 1_000_000),
        )
        self.assertAlmostEqual(
            2.5536,
            shadow_compare.calculate_cost_cny(model, 256_000, 1_000_000),
        )
        self.assertAlmostEqual(
            5.1072012,
            shadow_compare.calculate_cost_cny(model, 256_001, 1_000_000),
        )
        ledger = shadow_compare.BudgetLedger(20)
        payload = {"model": model, "messages": [], "max_tokens": 1_000_000}
        expected_tiers = (
            (32_000, "input_le_32k", 0.2, 0.8),
            (32_001, "input_32k_to_256k", 0.6, 2.4),
            (256_000, "input_32k_to_256k", 0.6, 2.4),
            (256_001, "input_gt_256k", 1.2, 4.8),
        )
        for input_tokens, tier, input_rate, output_rate in expected_tiers:
            record = ledger.record(
                "boundary:%d" % input_tokens, model,
                response("ok", input_tokens, 1_000_000), payload,
            )
            self.assertEqual(tier, record["pricing_tier"])
            self.assertEqual(input_rate, record["input_cny_per_million"])
            self.assertEqual(output_rate, record["output_cny_per_million"])

    def test_both_arms_use_one_frozen_evidence_hash(self):
        evidence = self._evidence()
        client = FakeClient()
        ledger = shadow_compare.BudgetLedger(3)
        with mock.patch.object(
            shadow_compare.full_refresh, "_public_http_url", return_value=True,
        ):
            qwen_only, verdict, mixed = shadow_compare.execute_arms(
                self.artist, evidence, client, ledger,
            )
        self.assertEqual(evidence["evidence_hash"], qwen_only["evidence_hash"])
        self.assertEqual(evidence["evidence_hash"], mixed["evidence_hash"])
        self.assertEqual(verdict_document(), verdict)
        for call in client.calls:
            prompt = json.dumps(call["payload"], ensure_ascii=False)
            self.assertIn(evidence["evidence_hash"], prompt)

    def test_candidate_rejects_url_not_in_frozen_evidence(self):
        evidence = self._evidence()
        document = final_document()
        document["research"]["sources"] = [{
            "category": "official",
            "title": "Invented source",
            "url": "https://invented.example/show",
        }]
        with self.assertRaises(shadow_compare.ShadowError):
            shadow_compare.validate_candidate(
                document, self.artist, evidence, "qwen_only",
            )

    def test_unknown_report_citation_is_rejected(self):
        evidence = self._evidence()
        document = final_document()
        document["daily_report"] = "- 没有可确认变化。[S999]"
        with mock.patch.object(
            shadow_compare.full_refresh, "_public_http_url", return_value=True,
        ), self.assertRaises(shadow_compare.ShadowError):
            shadow_compare.validate_candidate(
                document, self.artist, evidence, "qwen_only",
            )

    def test_full_fake_run_writes_only_external_sanitized_artifacts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "run"
            output.mkdir()
            before = shadow_compare.snapshot_production_tree()
            client = FakeClient()
            ledger = shadow_compare.BudgetLedger(3)
            fetcher = lambda url: {
                "access": "fetched",
                "final_url": url,
                "content_type": "text/html",
                "content_sha256": "a" * 64,
                "excerpt": "Mock public ticket evidence.",
                "truncated": False,
            }
            with mock.patch.object(
                shadow_compare.full_refresh, "_public_http_url", return_value=True,
            ), mock.patch.object(
                shadow_compare.full_refresh, "run_pipeline",
                side_effect=AssertionError("production pipeline called"),
            ), mock.patch.object(
                shadow_compare.monitor, "cmd_check",
                side_effect=AssertionError("monitor called"),
            ), mock.patch.object(
                shadow_compare.full_refresh, "_existing_context",
                return_value={"events": [], "rumors": []},
            ):
                acceptance = shadow_compare.run_shadow(
                    output, self.artist, 1, "2026-08-20", client, ledger,
                    baseline=None, fetcher=fetcher, explicit_secret=TEST_SECRET,
                )

            self.assertEqual(before, shadow_compare.snapshot_production_tree())
            self.assertIn(acceptance["status"], ("manual_review_required", "rejected"))
            expected = {
                "acceptance.json", "comparison.json", "comparison.md",
                "daily_mixed.md", "daily_qwen_only.md", "evidence.json",
                "glm_verdict.json", "manifest.json", "mixed_pipeline.json",
                "qwen_only.json",
            }
            self.assertEqual(expected, {path.name for path in output.iterdir()})
            all_text = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(TEST_SECRET, all_text)
            self.assertNotRegex(all_text, r"sk-[A-Za-z0-9._-]{12,}")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", manifest["status"])
            self.assertEqual(4, sum(
                item["purpose"].startswith("search:") for item in manifest["usage"]
            ))

    def test_collect_cli_only_searches_and_freezes_external_evidence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "collected"
            client = SearchOnlyFakeClient()
            before = shadow_compare.snapshot_production_tree()
            fetched = {
                "access": "fetched",
                "final_url": PUBLIC_URL,
                "content_type": "text/html",
                "content_sha256": "a" * 64,
                "excerpt": "Mock public ticket evidence.",
                "truncated": False,
            }
            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": TEST_SECRET}, clear=True,
            ), mock.patch.object(
                shadow_compare, "_artist_by_key", return_value=(self.artist, 1),
            ), mock.patch.object(
                shadow_compare, "DashScopeClient", return_value=client,
            ), mock.patch.object(
                shadow_compare, "fetch_public_source", return_value=fetched,
            ), mock.patch.object(
                shadow_compare.full_refresh, "_public_http_url", return_value=True,
            ), mock.patch.object(
                shadow_compare.full_refresh, "_existing_context",
                return_value={"events": [], "rumors": []},
            ):
                code = shadow_compare.main([
                    "collect", "--artist-key", "mock",
                    "--as-of", "2026-08-20", "--output-dir", str(output),
                ])

            self.assertEqual(0, code)
            self.assertEqual(before, shadow_compare.snapshot_production_tree())
            self.assertEqual(
                {"evidence.json", "manifest.json"},
                {path.name for path in output.iterdir()},
            )
            evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
            shadow_compare.verify_evidence_hash(evidence)
            self.assertEqual(4, len(evidence["queries"]))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("collect", manifest["mode"])
            self.assertEqual("completed", manifest["status"])
            self.assertTrue(manifest["search_only"])
            self.assertFalse(manifest["finalizers_executed"])
            self.assertEqual(
                {"search": shadow_compare.DEFAULT_SEARCH_MODEL}, manifest["models"],
            )
            self.assertEqual(4, len(manifest["usage"]))
            self.assertEqual(4, manifest["search_calls"])
            self.assertGreater(manifest["actual_cost_cny"], 0)
            self.assertEqual(
                manifest["production_snapshot_before"],
                manifest["production_snapshot_after"],
            )
            self.assertTrue(manifest["repository_unchanged"])
            self.assertTrue(all(
                item["purpose"].startswith("search:") for item in manifest["usage"]
            ))
            all_text = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(TEST_SECRET, all_text)
            self.assertNotRegex(all_text, r"sk-[A-Za-z0-9._-]{12,}")

    def test_collect_failure_writes_redacted_closed_manifest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "failed-collect"
            output.mkdir()
            before = shadow_compare.snapshot_production_tree()
            client = FailingSearchClient(fail_on_attempt=2)
            ledger = shadow_compare.BudgetLedger(3)
            fetcher = lambda url: {
                "access": "fetched",
                "final_url": url,
                "content_type": "text/html",
                "content_sha256": "a" * 64,
                "excerpt": "Mock public ticket evidence.",
                "truncated": False,
            }
            with mock.patch.object(
                shadow_compare.full_refresh, "_public_http_url", return_value=True,
            ), mock.patch.object(
                shadow_compare.full_refresh, "_existing_context",
                return_value={"events": [], "rumors": []},
            ), self.assertRaises(shadow_compare.ShadowError):
                shadow_compare.collect_shadow(
                    output, self.artist, "2026-08-20", client, ledger,
                    fetcher=fetcher, explicit_secret=TEST_SECRET,
                )

            self.assertEqual(before, shadow_compare.snapshot_production_tree())
            self.assertEqual(["manifest.json"], sorted(
                path.name for path in output.iterdir()
            ))
            raw = (output / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn(TEST_SECRET, raw)
            self.assertNotRegex(raw, r"sk-[A-Za-z0-9._-]{12,}")
            manifest = json.loads(raw)
            self.assertEqual("failed", manifest["status"])
            self.assertEqual(1, len(manifest["usage"]))
            self.assertGreater(manifest["actual_cost_cny"], 0)
            self.assertEqual(1, manifest["search_calls"])
            self.assertTrue(manifest["repository_unchanged"])
            self.assertEqual(
                manifest["production_snapshot_before"],
                manifest["production_snapshot_after"],
            )

    def _evidence(self):
        queries = [{
            "category": category,
            "query": "query for " + category,
            "answer": "No supported result.",
            "source_ids": ["S001"],
        } for category in shadow_compare.full_refresh.SEARCH_CATEGORIES]
        packet = {
            "schema_version": 1,
            "artist": self.artist,
            "as_of": "2026-08-20",
            "collected_at": "2026-08-20T12:00:00+08:00",
            "existing_candidates": {"events": [], "rumors": []},
            "queries": queries,
            "sources": [{
                "id": "S001",
                "url": PUBLIC_URL,
                "title": "Mock ticket source",
                "search_snippet": "",
                "categories": list(shadow_compare.full_refresh.SEARCH_CATEGORIES),
                "tier": "primary",
                "access": "fetched",
                "final_url": PUBLIC_URL,
                "content_type": "text/html",
                "content_sha256": "a" * 64,
                "excerpt": "No upcoming event.",
                "truncated": False,
            }],
        }
        packet["evidence_hash"] = shadow_compare._sha256_text(
            shadow_compare._canonical_json(packet),
        )
        return packet


if __name__ == "__main__":
    unittest.main()
