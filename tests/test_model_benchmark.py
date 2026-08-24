import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import model_benchmark


SOURCE_URL = "https://www.ticketmaster.com/katseye-tickets/artist/3259344"
OTHER_PRIMARY_URL = "https://www.livenation.com/artist/K8vZ917Qx40/katseye-events"
MOONSHOT_SECRET = "sk-moonshot-benchmark-test-123456"
DASHSCOPE_SECRET = "sk-dashscope-benchmark-test-123456"


def final_document(note="只使用冻结证据。"):
    seed = json.loads(
        model_benchmark.DEFAULT_GOLD_PATH.read_text(encoding="utf-8"),
    )
    events = []
    for expected in seed["artists"]["katseye"]["expected_events"]:
        event_type = expected.get("event_type") or "tour"
        events.append({
            "url": SOURCE_URL,
            "tour_name": expected.get("tour_name", "" if event_type == "festival" else "WILDWORLD TOUR"),
            "title": expected.get("title") or "KATSEYE %s" % expected["date"],
            "city": expected["city"],
            "country": "",
            "venue": expected["venue"],
            "show_date": expected["date"],
            "show_time": expected.get("show_time", ""),
            "show_end_time": expected.get("show_end_time", ""),
            "event_type": event_type,
            "price": "",
            "ticket_tiers": [],
            "sale_status": expected.get("sale_status", ""),
            "sale_time": expected.get("sale_time", ""),
            "confidence": "confirmed",
            "note": note,
        })
    return {
        "research": {
            "events": events,
            "rumors": [],
            "sources": [{
                "category": "ticketing",
                "title": "KATSEYE official ticket page",
                "url": SOURCE_URL,
            }],
            "coverage": {
                "ticketing_checked": True,
                "official_checked": True,
                "china_region_checked": True,
                "rumors_checked": True,
                "summary": "已阅读四类冻结证据。",
            },
        },
        "daily_report": "- KATSEYE frozen itinerary reviewed.[S001]",
        "decision_notes": [note],
    }


def provider_response(document=None, model=""):
    document = document or final_document()
    if model in {"kimi-k3", "kimi/kimi-k3"}:
        usage = {
            "prompt_tokens": 1200,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 200},
            "completion_tokens_details": {"reasoning_tokens": 300},
        }
    else:
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "completion_tokens_details": {"reasoning_tokens": 100},
        }
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps(document, ensure_ascii=False),
            },
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


class FakeClient:
    def __init__(self, documents=None):
        self.calls = []
        self.documents = list(documents or [])

    def complete(self, payload):
        self.calls.append(copy.deepcopy(payload))
        document = self.documents.pop(0) if self.documents else final_document()
        return provider_response(document, payload["model"])


class MissingUsageClient(FakeClient):
    def complete(self, payload):
        response = super().complete(payload)
        response.pop("usage")
        return response


class TransportFailureClient(FakeClient):
    def complete(self, payload):
        self.calls.append(copy.deepcopy(payload))
        raise model_benchmark.ProviderError("mock transport failed before accounting")


class FailAfterOnePaidResponseClient(FakeClient):
    def complete(self, payload):
        if self.calls:
            self.calls.append(copy.deepcopy(payload))
            raise model_benchmark.ProviderError("mock failure after one paid response")
        return super().complete(payload)


class ModelBenchmarkTests(unittest.TestCase):
    def _evidence(self):
        artist = {
            "key": "katseye",
            "name": "KATSEYE",
            "region": "kpop",
            "aliases": ["KATSEYE"],
            "search_terms": ["KATSEYE WILDWORLD TOUR"],
        }
        packet = {
            "schema_version": 1,
            "artist": artist,
            "as_of": "2026-08-20",
            "collected_at": "2026-08-20T12:00:00+08:00",
            "existing_candidates": {"events": [], "rumors": []},
            "queries": [{
                "category": category,
                "query": "frozen " + category,
                "answer": "The frozen answer cites S001.",
                "source_ids": ["S001"],
            } for category in model_benchmark.FROZEN_SEARCH_CATEGORIES],
            "sources": [{
                "id": "S001",
                "url": SOURCE_URL,
                "title": "KATSEYE official ticket page",
                "search_snippet": "KATSEYE WILDWORLD TOUR frozen itinerary",
                "categories": list(
                    model_benchmark.FROZEN_SEARCH_CATEGORIES
                ),
                "tier": "primary",
                "access": "fetched",
                "final_url": SOURCE_URL,
                "content_type": "text/html",
                "content_sha256": "a" * 64,
                "excerpt": "KATSEYE WILDWORLD TOUR and Irvine festival frozen evidence.",
                "truncated": False,
            }],
        }
        packet["evidence_hash"] = model_benchmark._sha256_json(packet)
        return packet

    def _write_evidence(self, directory, evidence=None):
        path = Path(directory) / "evidence.json"
        value = evidence or self._evidence()
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def _verified_gold(self, evidence):
        gold = json.loads(
            model_benchmark.DEFAULT_GOLD_PATH.read_text(encoding="utf-8"),
        )
        gold["_meta"]["verification_status"] = "human_verified"
        gold["_meta"]["evidence_hash"] = evidence["evidence_hash"]
        gold["_meta"]["automated_scoring_profile"] = (
            model_benchmark.AUTOMATED_SCORING_PROFILE
        )
        gold["artists"] = {"katseye": gold["artists"]["katseye"]}
        gold["artists"]["katseye"].pop("verified_key_facts", None)
        for event in gold["artists"]["katseye"]["expected_events"]:
            event["source_urls"] = [SOURCE_URL]
        for key in (
            "legacy_kimi_metrics", "daily_report_expectations", "hard_failures",
        ):
            gold.pop(key, None)
        for key in (
            "minimum_total_score", "score_weights", "maximum_refresh_cost_cny",
        ):
            gold["acceptance_thresholds"].pop(key, None)
        return gold

    def _write_verified_gold(self, directory, evidence):
        path = Path(directory) / "human-verified-gold.json"
        path.write_text(
            json.dumps(self._verified_gold(evidence), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _load_inputs(self, directory):
        evidence_path = self._write_evidence(directory)
        evidence, resolved, evidence_file_hash = model_benchmark.load_evidence(
            evidence_path,
        )
        gold_path = self._write_verified_gold(directory, evidence)
        gold, artist_key, gold_hash = model_benchmark.load_gold(gold_path, evidence)
        return evidence, resolved, evidence_file_hash, gold, artist_key, gold_hash

    def _create_resume_failure(self, directory, name="resume-failure"):
        inputs = self._load_inputs(directory)
        (
            evidence, evidence_path, evidence_file_hash,
            gold, artist_key, gold_hash,
        ) = inputs
        output = Path(directory) / name
        client = FailAfterOnePaidResponseClient()
        with self.assertRaises(model_benchmark.ProviderError), mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_public_http_url", return_value=True,
        ):
            model_benchmark.run_benchmark(
                evidence, evidence_path, evidence_file_hash,
                gold, gold_hash, artist_key,
                {"dashscope": client}, repeats=3,
                ledger=model_benchmark.BudgetLedger(20),
                output_path=output,
                secrets_to_remove=(DASHSCOPE_SECRET,),
                kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                dashscope_kimi_only=True,
                dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
            )
        self.assertEqual(2, len(client.calls))
        return (*inputs, output)

    def _convert_failure_to_legacy(self, output):
        manifest_path = output / "manifest.json"
        runs_path = output / "runs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runs_document = json.loads(runs_path.read_text(encoding="utf-8"))
        manifest.pop("artifact_hash", None)
        manifest.pop("runs_artifact_hash", None)
        manifest.pop("benchmark_protocol", None)
        for key in model_benchmark.LEGACY_ROUTE_METADATA_ADDITIONS:
            if isinstance(manifest.get("kimi_route"), dict):
                manifest["kimi_route"].pop(key, None)
        old_snapshot = "a" * 64
        manifest["production_snapshot_before"] = old_snapshot
        manifest["production_snapshot_after"] = old_snapshot
        runs_document.pop("artifact_hash", None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        runs_path.write_text(
            json.dumps(runs_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest, runs_document

    def _score_document(self, document):
        evidence = self._evidence()
        gold = self._verified_gold(evidence)
        artist_key = evidence["artist"]["key"]
        with mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_public_http_url", return_value=True,
        ):
            validated = model_benchmark.validate_benchmark_candidate(
                document, evidence["artist"], evidence, "offline",
            )
        return model_benchmark.score_candidate(
            validated, evidence, gold["artists"][artist_key],
        )

    def test_load_evidence_rejects_content_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence = self._evidence()
            evidence["sources"][0]["excerpt"] = "tampered after hashing"
            path = self._write_evidence(tempdir, evidence)
            with self.assertRaises(model_benchmark.BenchmarkError):
                model_benchmark.load_evidence(path)

    def test_seed_or_different_evidence_gold_fails_closed(self):
        evidence = self._evidence()
        with self.assertRaises(model_benchmark.BenchmarkError):
            model_benchmark.load_gold(model_benchmark.DEFAULT_GOLD_PATH, evidence)
        with tempfile.TemporaryDirectory() as tempdir:
            gold = self._verified_gold(evidence)
            gold["_meta"]["evidence_hash"] = "f" * 64
            path = Path(tempdir) / "wrong-evidence-gold.json"
            path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(model_benchmark.BenchmarkError):
                model_benchmark.load_gold(path, evidence)

    def test_human_gold_rejects_unimplemented_natural_language_scoring_config(self):
        evidence = self._evidence()
        variants = {}
        gold = self._verified_gold(evidence)
        gold["artists"]["katseye"]["verified_key_facts"] = [{
            "fact_id": "unstructured", "claim": "natural language claim",
        }]
        variants["verified_key_facts"] = gold
        gold = self._verified_gold(evidence)
        gold["daily_report_expectations"] = {"required_assertions": ["prose"]}
        variants["daily_report_expectations"] = gold
        gold = self._verified_gold(evidence)
        gold["hard_failures"] = [{"id": "unmapped", "condition": "prose"}]
        variants["hard_failures"] = gold
        gold = self._verified_gold(evidence)
        gold["acceptance_thresholds"]["minimum_total_score"] = 90
        variants["minimum_total_score"] = gold
        gold = self._verified_gold(evidence)
        gold["acceptance_thresholds"]["score_weights"] = {"fact_accuracy": 40}
        variants["score_weights"] = gold

        with tempfile.TemporaryDirectory() as tempdir:
            for label, value in variants.items():
                with self.subTest(label=label):
                    path = Path(tempdir) / (label + ".json")
                    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(model_benchmark.BenchmarkError):
                        model_benchmark.load_gold(path, evidence)

    def test_human_gold_rejects_unknown_invalid_or_relaxed_thresholds(self):
        evidence = self._evidence()
        variants = []
        for key, value in (
            ("minimum_magic_quality", 1.0),
            ("minimum_event_recall", 1.1),
            ("minimum_event_recall", 0.94),
            ("minimum_primary_evidence_ratio_for_confirmed", 0.99),
            ("minimum_core_field_accuracy", 0.99),
            ("minimum_matched_field_completeness", 0.99),
            ("minimum_normalized_stability", 0.994),
            ("maximum_duplicates", 1),
            ("maximum_false_confirmed", 0.5),
        ):
            gold = self._verified_gold(evidence)
            gold["acceptance_thresholds"][key] = value
            variants.append(gold)
        with tempfile.TemporaryDirectory() as tempdir:
            for index, gold in enumerate(variants):
                path = Path(tempdir) / ("invalid-threshold-%d.json" % index)
                path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(model_benchmark.BenchmarkError):
                    model_benchmark.load_gold(path, evidence)

    def test_human_gold_requires_per_event_primary_source_mapping(self):
        evidence = self._evidence()
        gold = self._verified_gold(evidence)
        gold["artists"]["katseye"]["expected_events"][0].pop("source_urls")
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "missing-source-map.json"
            path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(model_benchmark.BenchmarkError):
                model_benchmark.load_gold(path, evidence)

    def test_human_gold_rejects_unknown_per_event_fields_instead_of_ignoring(self):
        evidence = self._evidence()
        gold = self._verified_gold(evidence)
        gold["artists"]["katseye"]["expected_events"][0][
            "manual_fact_that_must_not_be_ignored"
        ] = "required"
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "unknown-event-field.json"
            path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError,
                "unsupported unscored fields",
            ):
                model_benchmark.load_gold(path, evidence)

    def test_evidence_must_live_outside_repository(self):
        with self.assertRaises(model_benchmark.BenchmarkError):
            model_benchmark.ensure_external_input(
                model_benchmark.ROOT / "research" / "evidence.json",
            )

    def test_four_payloads_share_messages_and_pin_reasoning_controls(self):
        evidence = self._evidence()
        payloads = {
            spec.key: model_benchmark.build_payload(spec, evidence)
            for spec in model_benchmark.MODEL_SPECS
        }
        message_hashes = {
            model_benchmark._sha256_json(payload["messages"])
            for payload in payloads.values()
        }
        self.assertEqual(1, len(message_hashes))
        self.assertEqual("high", payloads["kimi_k3_high"]["reasoning_effort"])
        self.assertEqual(
            "qwen3.7-plus-2026-05-26",
            payloads["qwen37_non_thinking"]["model"],
        )
        self.assertFalse(payloads["qwen37_non_thinking"]["enable_thinking"])
        self.assertNotIn("reasoning_effort", payloads["qwen37_non_thinking"])
        self.assertTrue(
            payloads["qwen37_non_thinking"]["response_format"]["json_schema"]["strict"],
        )
        self.assertEqual(
            "qwen3.7-flash-2026-07-15",
            payloads["qwen37_flash_non_thinking"]["model"],
        )
        self.assertFalse(payloads["qwen37_flash_non_thinking"]["enable_thinking"])
        self.assertEqual("low", payloads["qwen38_low"]["reasoning_effort"])
        for payload in payloads.values():
            self.assertIn(evidence["evidence_hash"], json.dumps(payload, ensure_ascii=False))
            self.assertEqual(16000, payload["max_completion_tokens"])
            self.assertNotIn("temperature", payload)
            event_schema = payload["response_format"]["json_schema"]["schema"][
                "properties"
            ]["research"]["properties"]["events"]["items"]
            self.assertIn("event_type", event_schema["required"])
            self.assertIn("show_end_time", event_schema["required"])
        self.assertNotIn(
            "event_type",
            model_benchmark.FROZEN_EVENT_PROPERTIES,
        )

    def test_dashscope_moonshot_route_pins_exact_k3_model_and_max_effort(self):
        evidence = self._evidence()
        specs = model_benchmark.model_specs_for_kimi_route(
            model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
        )
        self.assertEqual(4, len(specs))
        control = specs[0]
        self.assertEqual("kimi_k3_max_via_dashscope", control.key)
        self.assertEqual("dashscope", control.provider)
        self.assertEqual("kimi/kimi-k3", control.model)
        self.assertEqual("max", control.reasoning_mode)
        self.assertEqual("Moonshot AI", control.inference_service_provider)
        self.assertEqual((20.0, 2.0, 100.0), (
            control.input_cny_per_million,
            control.cached_input_cny_per_million,
            control.output_cny_per_million,
        ))

        payloads = [model_benchmark.build_payload(spec, evidence) for spec in specs]
        self.assertEqual("kimi/kimi-k3", payloads[0]["model"])
        self.assertEqual("max", payloads[0]["reasoning_effort"])
        self.assertEqual({"type": "json_object"}, payloads[0]["response_format"])
        self.assertEqual(16000, payloads[0]["max_tokens"])
        self.assertNotIn("max_completion_tokens", payloads[0])
        self.assertEqual(1, len({
            model_benchmark._sha256_json(payload["messages"])
            for payload in payloads
        }))

    def test_dashscope_aliyun_k3_route_pins_model_high_and_strict_schema(self):
        evidence = self._evidence()
        specs = model_benchmark.model_specs_for_kimi_route(
            model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
        )
        self.assertEqual(4, len(specs))
        control = specs[0]
        self.assertEqual("kimi_k3_high_via_dashscope_aliyun", control.key)
        self.assertEqual("dashscope", control.provider)
        self.assertEqual("kimi-k3", control.model)
        self.assertEqual("high", control.reasoning_mode)
        self.assertEqual("Alibaba Cloud Model Studio", control.inference_service_provider)
        self.assertEqual((20.0, 2.0, 100.0), (
            control.input_cny_per_million,
            control.cached_input_cny_per_million,
            control.output_cny_per_million,
        ))

        payloads = [model_benchmark.build_payload(spec, evidence) for spec in specs]
        control_payload = payloads[0]
        self.assertEqual("kimi-k3", control_payload["model"])
        self.assertEqual("high", control_payload["reasoning_effort"])
        self.assertTrue(
            control_payload["response_format"]["json_schema"]["strict"],
        )
        self.assertEqual(16000, control_payload["max_completion_tokens"])
        self.assertNotIn("max_tokens", control_payload)
        self.assertEqual(1, len({
            model_benchmark._sha256_json(payload["messages"])
            for payload in payloads
        }))

        route = model_benchmark._kimi_route_metadata(
            model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
        )
        self.assertEqual("dashscope-aliyun-k3", route["route_id"])
        self.assertEqual("aliyun_model_studio_deployment", route["deployment_kind"])
        self.assertEqual("kimi-k3", route["exact_model_id"])
        self.assertEqual("high", route["reasoning_effort"])
        self.assertTrue(route["provider_strict_json_schema_available"])
        self.assertTrue(route["client_strict_schema_validation"])
        self.assertTrue(route["activation_status_is_route_specific"])
        self.assertTrue(route["activation_status_not_inferred_from_other_kimi_route"])
        self.assertIn("aliyun-kimi-k3", route["official_reference"])
        self.assertFalse(route["budgeted_paid_execution_allowed"])
        usage = {
            "input_tokens": 1000, "cached_input_tokens": 200,
            "output_tokens": 300,
        }
        self.assertEqual(
            model_benchmark.calculate_cost_cny(
                model_benchmark.KIMI_NATIVE_SPEC, usage,
            ),
            model_benchmark.calculate_cost_cny(control, usage),
        )

    def test_moonshot_base_is_pinned_to_china_rmb_endpoint(self):
        self.assertEqual(
            model_benchmark.DEFAULT_MOONSHOT_BASE,
            model_benchmark._safe_base("moonshot", model_benchmark.DEFAULT_MOONSHOT_BASE),
        )
        with self.assertRaises(model_benchmark.BenchmarkError):
            model_benchmark._safe_base("moonshot", "https://api.moonshot.ai/v1")

    def test_pinned_qwen_cache_rates_are_in_cost_accounting(self):
        qwen37 = model_benchmark.MODEL_BY_KEY["qwen37_non_thinking"]
        qwen38 = model_benchmark.MODEL_BY_KEY["qwen38_low"]
        low_boundary = {
            "input_tokens": 256_000,
            "cached_input_tokens": 256_000,
            "output_tokens": 1_000_000,
        }
        high_boundary = {
            "input_tokens": 256_001,
            "cached_input_tokens": 256_001,
            "output_tokens": 1_000_000,
        }
        self.assertAlmostEqual(
            8.1024, model_benchmark.calculate_cost_cny(qwen37, low_boundary),
        )
        self.assertAlmostEqual(
            24.3072012, model_benchmark.calculate_cost_cny(qwen37, high_boundary),
        )
        self.assertAlmostEqual(
            36.384, model_benchmark.calculate_cost_cny(qwen38, low_boundary),
        )

    def test_qwen_flash_pricing_tiers_include_both_boundaries(self):
        flash = model_benchmark.MODEL_BY_KEY["qwen37_flash_non_thinking"]

        def usage(input_tokens):
            return {
                "input_tokens": input_tokens,
                "cached_input_tokens": input_tokens,
                "output_tokens": 1_000_000,
            }

        self.assertAlmostEqual(
            0.80128, model_benchmark.calculate_cost_cny(flash, usage(32_000)),
        )
        self.assertAlmostEqual(
            2.40384012, model_benchmark.calculate_cost_cny(flash, usage(32_001)),
        )
        self.assertAlmostEqual(
            2.43072, model_benchmark.calculate_cost_cny(flash, usage(256_000)),
        )
        self.assertAlmostEqual(
            4.86144024, model_benchmark.calculate_cost_cny(flash, usage(256_001)),
        )

    def test_preflight_input_bound_uses_utf8_bytes_for_chinese_evidence(self):
        payload = {
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "演唱会冻结证据" * 100}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 16000,
        }
        serialized_bytes = len(
            model_benchmark._canonical_json(payload).encode("utf-8"),
        )
        self.assertEqual(
            serialized_bytes + 4096,
            model_benchmark._prompt_token_upper_bound(payload),
        )

    def test_provider_wire_bytes_match_preflight_serializer_for_large_payload(self):
        payload = {
            "model": "qwen3.7-plus-2026-05-26",
            "messages": [{"role": "user", "content": "证据"}],
            "large_object": {"field_%05d" % index: index for index in range(5000)},
        }
        body = json.dumps({
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode("utf-8")
        response = mock.MagicMock()
        response.read.return_value = body
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch.object(
            model_benchmark.urllib.request, "urlopen", return_value=context,
        ) as urlopen:
            client = model_benchmark.ProviderClient(
                "dashscope", DASHSCOPE_SECRET, model_benchmark.DEFAULT_DASHSCOPE_BASE,
            )
            client.complete(payload)
        request = urlopen.call_args.args[0]
        expected_wire = model_benchmark._canonical_json(payload).encode("utf-8")
        self.assertEqual(expected_wire, request.data)
        self.assertGreaterEqual(
            model_benchmark._prompt_token_upper_bound(payload), len(request.data),
        )

    def test_gold_score_has_perfect_benchmark_core_metrics(self):
        metrics = self._score_document(final_document())
        self.assertEqual(1.0, metrics["event_recall"])
        self.assertEqual(1.0, metrics["confirmed_event_recall"])
        self.assertEqual(1.0, metrics["confirmed_precision"])
        self.assertEqual(1.0, metrics["primary_source_ratio"])
        self.assertEqual(0, metrics["duplicates"])
        self.assertEqual(1.0, metrics["core_field_accuracy"])
        self.assertEqual(1.0, metrics["matched_field_completeness"])
        self.assertEqual(1.0, metrics["gold_field_recovery"])
        self.assertEqual([], metrics["unsupported_gold_fields"])

    def test_primary_support_requires_gold_allowed_event_url_not_only_primary_tier(self):
        evidence = self._evidence()
        evidence["sources"].append({
            **copy.deepcopy(evidence["sources"][0]),
            "id": "S002",
            "url": OTHER_PRIMARY_URL,
            "final_url": OTHER_PRIMARY_URL,
        })
        evidence["evidence_hash"] = model_benchmark._sha256_json({
            key: value for key, value in evidence.items() if key != "evidence_hash"
        })
        gold = self._verified_gold(evidence)
        document = final_document()
        document["research"]["events"][0]["url"] = OTHER_PRIMARY_URL
        document["research"]["sources"].append({
            "category": "official",
            "title": "Different primary page",
            "url": OTHER_PRIMARY_URL,
        })
        with mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_public_http_url", return_value=True,
        ):
            validated = model_benchmark.validate_benchmark_candidate(
                document, evidence["artist"], evidence, "wrong_primary",
            )
        metrics = model_benchmark.score_candidate(
            validated, evidence, gold["artists"]["katseye"],
        )
        self.assertEqual(32, metrics["confirmed_events"])
        self.assertEqual(31, metrics["primary_supported_confirmed"])
        self.assertLess(metrics["primary_source_ratio"], 1.0)

    def test_benchmark_scores_irvine_festival_type_and_shayiting_end_time(self):
        gold = json.loads(
            model_benchmark.DEFAULT_GOLD_PATH.read_text(encoding="utf-8"),
        )
        irvine = gold["artists"]["katseye"]["expected_events"][0]
        self.assertEqual("festival", irvine["event_type"])
        self.assertTrue(
            model_benchmark._field_result(
                "event_type", {"event_type": "festival"}, irvine,
            )
        )
        self.assertFalse(
            model_benchmark._field_result("event_type", {"event_type": "tour"}, irvine)
        )
        shayiting = gold["artists"]["shayiting"]["expected_events"][0]
        self.assertTrue(model_benchmark._field_result(
            "show_end_time", {"show_end_time": "21:40"}, shayiting,
        ))

    def test_default_core_accuracy_and_normalized_stability_are_hard_gates(self):
        perfect = self._score_document(final_document())
        drifted = copy.deepcopy(perfect)
        # Identity is unchanged; only the normalized show-time field changes.
        drifted["normalized_core_signatures"][0][6] = "13:00"
        runs = [
            {"status": "valid", "metrics": copy.deepcopy(perfect)},
            {"status": "valid", "metrics": copy.deepcopy(perfect)},
            {"status": "valid", "metrics": drifted},
        ]
        result = model_benchmark.aggregate_model(
            model_benchmark.MODEL_BY_KEY["qwen38_low"], runs, [], 3,
            thresholds={},  # Exercise defaults absent from the seed fixture.
        )
        self.assertEqual(1.0, result["stability"]["valid_run_pairwise_event_jaccard"])
        self.assertLess(
            result["stability"]["valid_run_pairwise_normalized_core_jaccard"], 0.995,
        )
        self.assertFalse(result["acceptance"]["hard_gates"]["normalized_core_stability"])

        inaccurate_runs = copy.deepcopy(runs)
        for run in inaccurate_runs:
            run["metrics"]["core_field_accuracy"] = 0.99
        inaccurate = model_benchmark.aggregate_model(
            model_benchmark.MODEL_BY_KEY["qwen38_low"], inaccurate_runs, [], 3,
            thresholds={},
        )
        self.assertFalse(inaccurate["acceptance"]["hard_gates"]["core_field_accuracy"])

        incomplete_runs = copy.deepcopy(runs)
        for run in incomplete_runs:
            run["metrics"]["matched_field_completeness"] = 0.99
        incomplete = model_benchmark.aggregate_model(
            model_benchmark.MODEL_BY_KEY["qwen38_low"], incomplete_runs, [], 3,
            thresholds={},
        )
        self.assertFalse(
            incomplete["acceptance"]["hard_gates"]["matched_field_completeness"],
        )

    def test_95_percent_recall_does_not_lower_matched_core_accuracy(self):
        metrics = self._score_document(final_document())
        metrics["event_recall"] = 0.95
        metrics["confirmed_event_recall"] = 0.95
        metrics["gold_field_recovery"] = 0.95
        # Every field on the 95% of matched events remains correct and present.
        metrics["core_field_accuracy"] = 1.0
        metrics["matched_field_accuracy"] = 1.0
        metrics["matched_field_completeness"] = 1.0
        result = model_benchmark.aggregate_model(
            model_benchmark.MODEL_BY_KEY["qwen37_non_thinking"],
            [{"status": "valid", "metrics": copy.deepcopy(metrics)} for _ in range(3)],
            [], 3, thresholds={},
        )
        self.assertTrue(result["acceptance"]["hard_gates"]["event_recall"])
        self.assertTrue(result["acceptance"]["hard_gates"]["core_field_accuracy"])

    def test_all_rumor_output_fails_confirmed_event_recall(self):
        document = final_document()
        for event in document["research"]["events"]:
            event["confidence"] = "rumor"
        metrics = self._score_document(document)
        self.assertEqual(1.0, metrics["event_recall"])
        self.assertEqual(0.0, metrics["confirmed_event_recall"])
        result = model_benchmark.aggregate_model(
            model_benchmark.MODEL_BY_KEY["qwen37_non_thinking"],
            [{"status": "valid", "metrics": copy.deepcopy(metrics)} for _ in range(3)],
            [], 3, thresholds={},
        )
        self.assertFalse(
            result["acceptance"]["hard_gates"]["confirmed_event_recall"],
        )

    def test_candidate_recall_must_not_be_below_same_batch_k3_control(self):
        evidence = self._evidence()
        gold = self._verified_gold(evidence)
        perfect = self._score_document(final_document())
        runs = []
        for spec in model_benchmark.MODEL_SPECS:
            metrics = copy.deepcopy(perfect)
            if spec.key == "qwen37_non_thinking":
                metrics["event_recall"] = 0.96
                metrics["confirmed_event_recall"] = 0.96
            runs.extend({
                "status": "valid", "model_key": spec.key,
                "metrics": copy.deepcopy(metrics),
            } for _ in range(3))
        scorecard = model_benchmark.build_scorecard(
            evidence, gold, runs, model_benchmark.BudgetLedger(20), repeats=3,
        )
        self.assertEqual(
            1.0,
            scorecard["effective_hard_gate_thresholds"]["minimum_core_field_accuracy"],
        )
        self.assertEqual(
            0.995,
            scorecard["effective_hard_gate_thresholds"]["minimum_normalized_stability"],
        )
        self.assertEqual(
            1.0,
            scorecard["effective_hard_gate_thresholds"][
                "minimum_matched_field_completeness"
            ],
        )
        coverage = scorecard["gold_scoring_coverage"]
        self.assertEqual(32, coverage["expected_events"])
        self.assertEqual(32, coverage["scored_field_denominators"]["date"])
        self.assertIn("sale_time", coverage["supported_but_unscored_fields"])
        markdown = model_benchmark._scorecard_markdown(scorecard)
        self.assertIn("Gold field coverage:", markdown)
        self.assertIn("Supported but unscored fields:", markdown)
        k3_gate = scorecard["models"]["kimi_k3_high"]["acceptance"]["hard_gates"]
        qwen_gate = scorecard["models"]["qwen37_non_thinking"]["acceptance"]["hard_gates"]
        self.assertTrue(k3_gate["recall_not_below_k3_control"])
        self.assertTrue(qwen_gate["event_recall"])
        self.assertFalse(qwen_gate["recall_not_below_k3_control"])

    def test_duplicate_and_false_confirmed_are_counted(self):
        evidence = self._evidence()
        gold = self._verified_gold(evidence)
        artist_key = evidence["artist"]["key"]
        document = final_document()
        duplicate = copy.deepcopy(document["research"]["events"][0])
        duplicate["city"] = " %s " % duplicate["city"]
        document["research"]["events"].append(duplicate)
        with mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_public_http_url", return_value=True,
        ):
            validated = model_benchmark.validate_benchmark_candidate(
                document, evidence["artist"], evidence, "duplicate",
            )
        metrics = model_benchmark.score_candidate(
            validated, evidence, gold["artists"][artist_key],
        )
        self.assertEqual(1, metrics["duplicates"])
        self.assertEqual(1, metrics["false_confirmed"])
        self.assertAlmostEqual(32 / 33, metrics["confirmed_precision"], places=6)

    def test_gold_matching_normalizes_bilingual_venue_and_known_session_time(self):
        candidate = {
            "show_date": "2026-09-11",
            "city": "澳门 / Macao",
            "venue": "The Venetian Arena / 澳门威尼斯人综艺馆",
            "show_time": "20:00",
        }
        expected = {
            "date": "2026-09-11",
            "city": "Macao",
            "city_aliases": ["澳门"],
            "venue": "The Venetian Arena",
            "show_time": "20:00",
        }
        self.assertTrue(model_benchmark._matches_gold(candidate, expected))
        self.assertTrue(model_benchmark._field_result("venue", candidate, expected))
        candidate["show_time"] = "18:00"
        self.assertFalse(model_benchmark._matches_gold(candidate, expected))

    def test_same_day_same_venue_different_show_times_are_not_duplicates(self):
        events = [{
            "show_date": "2026-09-19", "city": "Shanghai",
            "venue": "Example Arena", "show_time": "14:00",
        }, {
            "show_date": "2026-09-19", "city": "Shanghai",
            "venue": "Example Arena", "show_time": "19:30",
        }]
        self.assertEqual(0, model_benchmark._duplicate_count(events))
        events.append(copy.deepcopy(events[-1]))
        self.assertEqual(1, model_benchmark._duplicate_count(events))
        different_city = copy.deepcopy(events[-1])
        different_city["city"] = "Beijing"
        events.append(different_city)
        self.assertEqual(1, model_benchmark._duplicate_count(events))

    def test_duplicate_city_aliases_canonicalize_through_gold(self):
        expected = [{
            "date": "2026-09-05", "city": "Pasay",
            "city_aliases": ["Manila"], "venue": "SM Mall of Asia Arena",
        }]
        events = [{
            "show_date": "2026-09-05", "city": "Pasay",
            "venue": "SM Mall of Asia Arena", "show_time": "",
        }, {
            "show_date": "2026-09-05", "city": "Manila",
            "venue": "SM Mall of Asia Arena", "show_time": "",
        }]
        self.assertEqual(1, model_benchmark._duplicate_count(events, expected))

    def test_full_mock_benchmark_is_external_accounted_and_stable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "benchmark-output"
            moonshot = FakeClient()
            dashscope = FakeClient()
            before = model_benchmark.shadow_compare.snapshot_production_tree()
            with mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                scorecard, output_dir = model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"moonshot": moonshot, "dashscope": dashscope},
                    repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(MOONSHOT_SECRET, DASHSCOPE_SECRET),
                )

            self.assertEqual(output.resolve(), output_dir)
            self.assertEqual(before, model_benchmark.shadow_compare.snapshot_production_tree())
            self.assertEqual(3, len(moonshot.calls))
            self.assertEqual(9, len(dashscope.calls))
            self.assertGreater(scorecard["estimated_list_cost_cny"], 0)
            self.assertEqual(
                "katseye_single_artist_finalizer_pilot_only", scorecard["scope"],
            )
            self.assertEqual("pending_manual_review", scorecard["selection_status"])
            self.assertIn("different compute", scorecard["comparison_caveat"])
            sampling = scorecard["generation_sampling_control"]
            self.assertEqual(
                "provider_default_not_sent", sampling["temperature_request_mode"],
            )
            self.assertFalse(sampling["uniform_temperature_zero_supported"])
            self.assertIn("fixed at 1.0", sampling["limitation"])
            self.assertIn("below 0.6", sampling["limitation"])
            self.assertEqual(4, len(scorecard["models"]))
            self.assertIn("full_12_artist_refresh", scorecard["excluded_costs"])
            self.assertEqual(4, len(scorecard["manual_review_required"]))
            for spec in model_benchmark.MODEL_SPECS:
                result = scorecard["models"][spec.key]
                self.assertEqual(3, result["valid_runs"])
                self.assertEqual(1.0, result["metrics"]["event_recall"]["mean"])
                self.assertEqual(
                    1.0, result["stability"]["validity_adjusted_event_stability"],
                )
                self.assertEqual(
                    1.0,
                    result["stability"][
                        "validity_adjusted_normalized_core_stability"
                    ],
                )
                self.assertEqual("passed", result["acceptance"]["status"])
            self.assertEqual(
                {"manifest.json", "runs.json", "scorecard.json", "scorecard.md"},
                {path.name for path in output.iterdir()},
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["production_tree_unchanged"])
            self.assertEqual(12, len(manifest["usage"]))
            self.assertTrue(all(
                item["temperature_request_mode"] == "provider_default_not_sent"
                for item in manifest["models"]
            ))
            self.assertTrue(all(
                "temperature" not in payload
                for payload in moonshot.calls + dashscope.calls
            ))
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(MOONSHOT_SECRET, combined)
            self.assertNotIn(DASHSCOPE_SECRET, combined)

    def test_qwen_only_mock_screen_is_accounted_but_awaits_k3_control(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "qwen-only-output"
            dashscope = FakeClient()
            before = model_benchmark.shadow_compare.snapshot_production_tree()
            with mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                scorecard, output_dir = model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    qwen_only=True,
                )

            self.assertEqual(output.resolve(), output_dir)
            self.assertEqual(before, model_benchmark.shadow_compare.snapshot_production_tree())
            self.assertEqual(9, len(dashscope.calls))
            self.assertEqual(
                1,
                len({
                    model_benchmark._sha256_json(payload["messages"])
                    for payload in dashscope.calls
                }),
            )
            self.assertEqual(
                model_benchmark.BENCHMARK_MODE_QWEN_ONLY,
                scorecard["benchmark_mode"],
            )
            self.assertEqual("awaiting_k3_control", scorecard["selection_status"])
            self.assertFalse(scorecard["production_authorized"])
            self.assertFalse(scorecard["k3_control"]["included"])
            self.assertEqual(
                "recall_not_below_control",
                scorecard["k3_control"]["missing_hard_gate"],
            )
            self.assertEqual(
                [spec.key for spec in model_benchmark.QWEN_MODEL_SPECS],
                scorecard["model_order"],
            )
            self.assertGreater(scorecard["estimated_list_cost_cny"], 0)
            for spec in model_benchmark.QWEN_MODEL_SPECS:
                result = scorecard["models"][spec.key]
                self.assertEqual(3, result["valid_runs"])
                self.assertEqual(
                    "passed", result["acceptance"]["gold_quality_gate_status"],
                )
                self.assertEqual(
                    "awaiting_k3_control", result["acceptance"]["status"],
                )
                self.assertFalse(
                    result["acceptance"]["production_selection_eligible"],
                )
                self.assertIsNone(
                    result["acceptance"]["hard_gates"][
                        "recall_not_below_k3_control"
                    ],
                )
                self.assertIsNone(
                    result["acceptance"]["hard_gates"][
                        "recall_not_below_control"
                    ],
                )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(model_benchmark.BENCHMARK_MODE_QWEN_ONLY, manifest["benchmark_mode"])
            self.assertIsNone(manifest["kimi_route"])
            self.assertFalse(manifest["k3_control"]["included"])
            self.assertTrue(manifest["production_tree_unchanged"])
            self.assertEqual(9, len(manifest["usage"]))
            markdown = (output / "scorecard.md").read_text(encoding="utf-8")
            self.assertIn("awaiting_k3_control", markdown)
            self.assertIn("cannot authorize production selection", markdown)
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(DASHSCOPE_SECRET, combined)

    def test_qwen_only_global_budget_fails_before_output_or_paid_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "qwen-only-must-not-exist"
            dashscope = FakeClient()
            with self.assertRaises(model_benchmark.BudgetExceeded):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(0.001),
                    output_path=output,
                    qwen_only=True,
                )
            self.assertFalse(output.exists())
            self.assertEqual([], dashscope.calls)

    def test_dashscope_moonshot_route_is_explicit_and_paid_run_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "dashscope-k3-output"
            dashscope = FakeClient()
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError,
                "hidden reasoning tokens",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
                )

            self.assertEqual([], dashscope.calls)
            self.assertFalse(output.exists())
            route = model_benchmark._kimi_route_metadata(
                model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
            )
            self.assertEqual("dashscope-moonshot", route["route_id"])
            self.assertEqual("kimi/kimi-k3", route["exact_model_id"])
            self.assertEqual("max", route["reasoning_effort"])
            self.assertEqual("Moonshot AI", route["inference_service_provider"])
            self.assertTrue(route["routed_via_bailian"])
            self.assertFalse(route["matches_current_production_k3_high"])
            self.assertEqual(
                "json_object_with_client_schema_validation",
                route["provider_response_format"],
            )
            self.assertFalse(route["documented_total_completion_token_cap"])
            self.assertFalse(route["budgeted_paid_execution_allowed"])
            self.assertIn("not equivalent", route["production_configuration_limitation"])

    def test_dashscope_kimi_only_explicit_confirmation_is_accounted_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "dashscope-kimi-control-only"
            dashscope = FakeClient()
            with mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                scorecard, output_dir = model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                )

            self.assertEqual(output.resolve(), output_dir)
            self.assertEqual(3, len(dashscope.calls))
            self.assertTrue(all(
                payload["model"] == "kimi/kimi-k3"
                and payload["reasoning_effort"] == "max"
                and payload["response_format"] == {"type": "json_object"}
                and "max_completion_tokens" not in payload
                for payload in dashscope.calls
            ))
            qwen_messages_hash = model_benchmark._sha256_json(
                model_benchmark.build_payload(
                    model_benchmark.QWEN_MODEL_SPECS[0], evidence,
                )["messages"]
            )
            self.assertTrue(all(
                model_benchmark._sha256_json(payload["messages"])
                == qwen_messages_hash
                for payload in dashscope.calls
            ))
            self.assertEqual(
                model_benchmark.BENCHMARK_MODE_DASHSCOPE_KIMI_ONLY,
                scorecard["benchmark_mode"],
            )
            self.assertEqual("control_only_incomplete", scorecard["selection_status"])
            self.assertFalse(scorecard["production_authorized"])
            self.assertEqual(
                "control_only_incomplete",
                scorecard["models"]["kimi_k3_max_via_dashscope"]["acceptance"][
                    "status"
                ],
            )
            route = scorecard["kimi_route"]
            self.assertEqual("max", route["reasoning_effort"])
            self.assertFalse(route["matches_current_production_k3_high"])
            self.assertFalse(route["provider_strict_json_schema_available"])
            self.assertTrue(route["client_strict_schema_validation"])
            self.assertFalse(route["max_cost_cny_hard_caps_first_k3_request"])
            self.assertTrue(route["account_level_budget_cap_confirmed"])
            self.assertTrue(route["budgeted_paid_execution_allowed"])

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                model_benchmark.BENCHMARK_MODE_DASHSCOPE_KIMI_ONLY,
                manifest["benchmark_mode"],
            )
            self.assertEqual(3, len(manifest["usage"]))
            self.assertEqual(0, manifest["worst_case_reserved_cny"])
            self.assertEqual(
                "all_calls_except_dashscope_kimi_hidden_reasoning",
                manifest["budget_preflight"]["hard_preflight_coverage"],
            )
            self.assertEqual(
                "account_level_cap_only",
                manifest["budget_preflight"][
                    "dashscope_kimi_first_call_budget_protection"
                ],
            )
            self.assertEqual(scorecard["prompt_hash"], manifest["prompt_hash"])
            self.assertEqual(
                scorecard["benchmark_result_schema_hash"],
                manifest["benchmark_result_schema_hash"],
            )
            expected_contract = model_benchmark._comparison_contract(
                evidence, gold, qwen_messages_hash, 3,
            )
            self.assertEqual(expected_contract, scorecard["comparison_contract"])
            self.assertEqual(expected_contract, manifest["comparison_contract"])
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(DASHSCOPE_SECRET, combined)

    def test_dashscope_aliyun_k3_only_is_fail_closed_then_strict_and_accounted(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "dashscope-aliyun-k3-control-only"
            dashscope = FakeClient()
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError, "cannot hard-cap the first charge",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                )
            self.assertEqual([], dashscope.calls)
            self.assertFalse(output.exists())

            with mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                scorecard, _ = model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                )

            self.assertEqual(3, len(dashscope.calls))
            self.assertTrue(all(
                payload["model"] == "kimi-k3"
                and payload["reasoning_effort"] == "high"
                and payload["response_format"]["json_schema"]["strict"]
                and "max_tokens" not in payload
                for payload in dashscope.calls
            ))
            qwen_messages_hash = model_benchmark._sha256_json(
                model_benchmark.build_payload(
                    model_benchmark.QWEN_MODEL_SPECS[0], evidence,
                )["messages"]
            )
            self.assertTrue(all(
                model_benchmark._sha256_json(payload["messages"])
                == qwen_messages_hash
                for payload in dashscope.calls
            ))
            self.assertEqual("control_only_incomplete", scorecard["selection_status"])
            self.assertFalse(scorecard["production_authorized"])
            route = scorecard["kimi_route"]
            self.assertEqual("dashscope-aliyun-k3", route["route_id"])
            self.assertEqual("Alibaba Cloud Model Studio", route["inference_service_provider"])
            self.assertFalse(route["matches_current_production_k3_high"])
            self.assertTrue(route["provider_strict_json_schema_available"])
            self.assertTrue(route["client_strict_schema_validation"])
            self.assertTrue(route["account_level_budget_cap_confirmed"])
            self.assertIn("not fully equivalent", route["production_configuration_limitation"])
            self.assertIn("separate product", scorecard["comparison_caveat"])
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(3, len(manifest["usage"]))
            self.assertEqual(scorecard["comparison_contract"], manifest["comparison_contract"])
            self.assertEqual(
                model_benchmark._comparison_contract(
                    evidence, gold, qwen_messages_hash, 3,
                ),
                manifest["comparison_contract"],
            )
            self.assertEqual(
                "Alibaba Cloud Model Studio kimi-k3 deployment",
                manifest["kimi_route"]["activation_product_identity"],
            )
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(DASHSCOPE_SECRET, combined)

    def test_frozen_benchmark_protocol_does_not_inherit_live_production_schema(self):
        evidence = self._evidence()
        expected_schema_hash = (
            "f07fc1484e281fb307cf8d313de45e39f3f130ac3317460979b4a39340354420"
        )
        prompt_before = model_benchmark._sha256_json(
            model_benchmark.build_payload(
                model_benchmark.QWEN_MODEL_SPECS[0], evidence,
            )["messages"]
        )
        with mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "EVENT_PROPERTIES",
            {"source_id": {"type": "string"}},
        ), mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_validate_schema",
            side_effect=AssertionError("live validator must not be called"),
        ), mock.patch.object(
            model_benchmark.shadow_compare.full_refresh,
            "_url_identity",
            side_effect=AssertionError("live URL semantics must not be called"),
        ):
            validated = model_benchmark.validate_benchmark_candidate(
                final_document(), evidence["artist"], evidence, "frozen",
            )
            prompt_after = model_benchmark._sha256_json(
                model_benchmark.build_payload(
                    model_benchmark.QWEN_MODEL_SPECS[0], evidence,
                )["messages"]
            )
        self.assertTrue(validated["research"]["events"])
        self.assertEqual(prompt_before, prompt_after)
        self.assertEqual(
            expected_schema_hash,
            model_benchmark._sha256_json(model_benchmark.BENCHMARK_RESULT_SCHEMA),
        )
        self.assertEqual(
            "katseye_finalizer_legacy_url_sources_v1",
            model_benchmark.FROZEN_BENCHMARK_PROTOCOL,
        )

    def test_legacy_failed_checkpoint_must_be_sealed_then_resumes_missing_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            created = self._create_resume_failure(tempdir, "legacy-resume")
            *inputs, output = created
            legacy_manifest, _ = self._convert_failure_to_legacy(output)
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = inputs

            no_calls = FakeClient()
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError, "seal-legacy-failed-checkpoint",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": no_calls}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                    resume=True,
                )
            self.assertEqual([], no_calls.calls)

            raw_manifest_sha = model_benchmark._sha256_bytes(
                (output / "manifest.json").read_bytes()
            )
            raw_runs_sha = model_benchmark._sha256_bytes(
                (output / "runs.json").read_bytes()
            )
            sealed, sealed_dir = model_benchmark.seal_legacy_failed_checkpoint(
                evidence, evidence_path, evidence_file_hash,
                gold, gold_hash, artist_key, output, 3, 20,
                kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                dashscope_kimi_only=True,
                dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
            )
            self.assertEqual(output.resolve(), sealed_dir)
            self.assertEqual("failed", sealed["status"])
            self.assertEqual(
                model_benchmark.FROZEN_BENCHMARK_PROTOCOL,
                sealed["benchmark_protocol"],
            )
            audit = sealed["legacy_checkpoint_import"]
            self.assertEqual(raw_manifest_sha, audit["original_manifest_file_sha256"])
            self.assertEqual(raw_runs_sha, audit["original_runs_file_sha256"])
            self.assertEqual(
                legacy_manifest["production_snapshot_before"],
                audit["original_production_snapshot"],
            )
            self.assertEqual(0, audit["provider_requests_sent"])
            self.assertFalse(audit["api_keys_read"])
            self.assertFalse(audit["legacy_provenance_cryptographically_authenticated"])
            current_snapshot = model_benchmark.shadow_compare.snapshot_digest(
                model_benchmark.shadow_compare.snapshot_production_tree()
            )
            self.assertEqual(current_snapshot, sealed["production_snapshot_before"])

            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError, "already sealed",
            ):
                model_benchmark.seal_legacy_failed_checkpoint(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key, output, 3, 20,
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                )

            resumed_client = FakeClient()
            scorecard, _ = model_benchmark.run_benchmark(
                evidence, evidence_path, evidence_file_hash,
                gold, gold_hash, artist_key,
                {"dashscope": resumed_client}, repeats=3,
                ledger=model_benchmark.BudgetLedger(20),
                output_path=output,
                kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                dashscope_kimi_only=True,
                dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                resume=True,
            )
            self.assertEqual(2, len(resumed_client.calls))
            self.assertEqual("control_only_incomplete", scorecard["selection_status"])
            final_manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(3, len(final_manifest["usage"]))
            self.assertEqual(
                audit["original_manifest_file_sha256"],
                final_manifest["legacy_checkpoint_import"][
                    "original_manifest_file_sha256"
                ],
            )

    def test_legacy_seal_cli_is_offline_and_does_not_require_api_keys(self):
        with tempfile.TemporaryDirectory() as tempdir:
            created = self._create_resume_failure(tempdir, "legacy-cli")
            *inputs, output = created
            self._convert_failure_to_legacy(output)
            evidence_path = inputs[1]
            gold_path = Path(tempdir) / "human-verified-gold.json"
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                model_benchmark, "ProviderClient",
            ) as provider_class:
                code = model_benchmark.main([
                    "--evidence", str(evidence_path),
                    "--gold", str(gold_path),
                    "--output-dir", str(output),
                    "--repeats", "3",
                    "--max-cost-cny", "20",
                    "--timeout", "900",
                    "--kimi-route", "dashscope-aliyun-k3",
                    "--dashscope-kimi-only",
                    "--confirm-dashscope-kimi-uncapped-hidden-reasoning-account-cap",
                    "--seal-legacy-failed-checkpoint",
                ])
            self.assertEqual(0, code)
            provider_class.assert_not_called()
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn("legacy_checkpoint_import", manifest)

    def test_legacy_seal_rejects_pending_tamper_secret_and_unexpected_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            for name in ("pending", "metrics", "secret", "unexpected"):
                with self.subTest(name=name):
                    created = self._create_resume_failure(tempdir, "legacy-" + name)
                    *inputs, output = created
                    manifest, runs_document = self._convert_failure_to_legacy(output)
                    if name == "pending":
                        runs_document["runs"][0]["status"] = (
                            "paid_response_accounted_pending_validation"
                        )
                    elif name == "metrics":
                        runs_document["runs"][0]["metrics"]["event_recall"] = 0.123
                    elif name == "secret":
                        manifest["failure"]["message"] = DASHSCOPE_SECRET
                    else:
                        manifest["unexpected"] = "not allowed"
                    (output / "manifest.json").write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    (output / "runs.json").write_text(
                        json.dumps(runs_document, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    (
                        evidence, evidence_path, evidence_file_hash,
                        gold, artist_key, gold_hash,
                    ) = inputs
                    with self.assertRaises(model_benchmark.BenchmarkError):
                        model_benchmark.seal_legacy_failed_checkpoint(
                            evidence, evidence_path, evidence_file_hash,
                            gold, gold_hash, artist_key, output, 3, 20,
                            secrets_to_remove=(DASHSCOPE_SECRET,),
                            kimi_route=(
                                model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3
                            ),
                            dashscope_kimi_only=True,
                            dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                        )

    def test_resume_failed_k3_only_preserves_first_charge_and_runs_missing_repeats(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash, output,
            ) = self._create_resume_failure(tempdir)
            failed_manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            first_usage = copy.deepcopy(failed_manifest["usage"][0])
            resumed_client = FakeClient()
            with mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                scorecard, output_dir = model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": resumed_client}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                    resume=True,
                )
            self.assertEqual(output.resolve(), output_dir)
            self.assertEqual(2, len(resumed_client.calls))
            self.assertEqual("control_only_incomplete", scorecard["selection_status"])
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            runs_document = json.loads((output / "runs.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", manifest["status"])
            self.assertEqual(1, manifest["resume_count"])
            self.assertEqual(1, len(manifest["resume_history"]))
            self.assertEqual(3, len(manifest["usage"]))
            self.assertEqual(first_usage, manifest["usage"][0])
            self.assertEqual([1, 2, 3], [run["repeat"] for run in runs_document["runs"]])
            self.assertTrue(all(run["status"] == "valid" for run in runs_document["runs"]))
            manifest_copy = copy.deepcopy(manifest)
            manifest_hash = manifest_copy.pop("artifact_hash")
            self.assertEqual(model_benchmark._sha256_json(manifest_copy), manifest_hash)
            runs_copy = copy.deepcopy(runs_document)
            runs_hash = runs_copy.pop("artifact_hash")
            self.assertEqual(model_benchmark._sha256_json(runs_copy), runs_hash)
            self.assertEqual(runs_hash, manifest["runs_artifact_hash"])

            no_calls = FakeClient()
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError, "completed benchmark",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": no_calls}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                    resume=True,
                )
            self.assertEqual([], no_calls.calls)

    def test_resume_rejects_pending_duplicate_out_of_order_and_tampered_runs(self):
        def attempt(inputs, output):
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = inputs
            no_calls = FakeClient()
            with self.assertRaises(model_benchmark.BenchmarkError), mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": no_calls}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                    resume=True,
                )
            self.assertEqual([], no_calls.calls)

        with tempfile.TemporaryDirectory() as tempdir:
            for name in ("pending", "duplicate", "metrics", "usage"):
                with self.subTest(name=name):
                    created = self._create_resume_failure(tempdir, "resume-" + name)
                    *inputs, output = created
                    manifest = json.loads(
                        (output / "manifest.json").read_text(encoding="utf-8")
                    )
                    runs_document = json.loads(
                        (output / "runs.json").read_text(encoding="utf-8")
                    )
                    runs = runs_document["runs"]
                    if name == "pending":
                        runs[0]["status"] = "paid_response_accounted_pending_validation"
                    elif name == "duplicate":
                        runs.append(copy.deepcopy(runs[0]))
                        manifest["usage"].append(copy.deepcopy(manifest["usage"][0]))
                        manifest["estimated_list_cost_cny"] = round(
                            2 * manifest["usage"][0]["estimated_list_cost_cny"], 8,
                        )
                    elif name == "metrics":
                        runs[0]["metrics"]["event_recall"] = 0.123456
                    else:
                        runs[0]["usage"].pop("reasoning_tokens")
                    model_benchmark._checkpoint_artifacts(
                        output, manifest, inputs[0]["evidence_hash"], runs, (),
                    )
                    attempt(tuple(inputs), output)

    def test_resume_rejects_manifest_mismatch_secret_and_unsafe_permissions(self):
        def resume_without_calls(inputs, output):
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = inputs
            client = FakeClient()
            with self.assertRaises(model_benchmark.BenchmarkError):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": client}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                    resume=True,
                )
            self.assertEqual([], client.calls)

        with tempfile.TemporaryDirectory() as tempdir:
            for name, field, value in (
                ("prompt", "prompt_hash", "0" * 64),
                ("schema", "benchmark_result_schema_hash", "1" * 64),
                ("route", "benchmark_mode", model_benchmark.BENCHMARK_MODE_FULL),
                ("repeats", "repeats", 4),
                ("production", "production_snapshot_before", "2" * 64),
            ):
                with self.subTest(name=name):
                    created = self._create_resume_failure(tempdir, "mismatch-" + name)
                    *inputs, output = created
                    manifest = json.loads(
                        (output / "manifest.json").read_text(encoding="utf-8")
                    )
                    runs = json.loads(
                        (output / "runs.json").read_text(encoding="utf-8")
                    )["runs"]
                    manifest[field] = value
                    model_benchmark._checkpoint_artifacts(
                        output, manifest, inputs[0]["evidence_hash"], runs, (),
                    )
                    resume_without_calls(tuple(inputs), output)

            created = self._create_resume_failure(tempdir, "secret")
            *inputs, output = created
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            manifest["failure"]["message"] = DASHSCOPE_SECRET
            manifest.pop("artifact_hash", None)
            manifest["artifact_hash"] = model_benchmark._sha256_json(manifest)
            (output / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            resume_without_calls(tuple(inputs), output)

            created = self._create_resume_failure(tempdir, "permissions")
            *inputs, output = created
            output.chmod(0o777)
            try:
                resume_without_calls(tuple(inputs), output)
            finally:
                output.chmod(0o755)

    def test_checkpoint_redaction_happens_before_integrity_hashes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            created = self._create_resume_failure(tempdir, "redacted-checkpoint")
            *inputs, output = created
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            runs = json.loads(
                (output / "runs.json").read_text(encoding="utf-8")
            )["runs"]
            manifest["failure"]["message"] = "provider echoed " + DASHSCOPE_SECRET
            model_benchmark._checkpoint_artifacts(
                output, manifest, inputs[0]["evidence_hash"], runs,
                (DASHSCOPE_SECRET,),
            )
            manifest_document = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            runs_document = json.loads(
                (output / "runs.json").read_text(encoding="utf-8")
            )
            combined = json.dumps(
                [manifest_document, runs_document], ensure_ascii=False,
            )
            self.assertNotIn(DASHSCOPE_SECRET, combined)
            manifest_hash = manifest_document.pop("artifact_hash")
            self.assertEqual(
                model_benchmark._sha256_json(manifest_document), manifest_hash,
            )
            runs_hash = runs_document.pop("artifact_hash")
            self.assertEqual(model_benchmark._sha256_json(runs_document), runs_hash)
            self.assertEqual(runs_hash, manifest_document["runs_artifact_hash"])

    def test_dashscope_kimi_only_requires_route_and_confirmation_before_calls(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            dashscope = FakeClient()
            output = Path(tempdir) / "must-not-exist"
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError, "requires --kimi-route",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                )
            self.assertEqual([], dashscope.calls)
            self.assertFalse(output.exists())

    def test_dashscope_kimi_over_threshold_checkpoints_incurred_usage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "dashscope-kimi-over-threshold"
            dashscope = FakeClient()
            with self.assertRaises(model_benchmark.BudgetExceeded), mock.patch.object(
                model_benchmark.shadow_compare.full_refresh,
                "_public_http_url", return_value=True,
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(0.01),
                    output_path=output,
                    secrets_to_remove=(DASHSCOPE_SECRET,),
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
                    dashscope_kimi_only=True,
                    dashscope_kimi_unbounded_reasoning_account_cap_confirmed=True,
                )
            self.assertEqual(1, len(dashscope.calls))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest["status"])
            self.assertEqual(1, len(manifest["usage"]))
            self.assertGreater(manifest["estimated_list_cost_cny"], 0.01)
            runs = json.loads((output / "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(runs["runs"]))
            self.assertEqual(
                "paid_response_accounted_budget_exceeded",
                runs["runs"][0]["status"],
            )
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in output.iterdir()
            )
            self.assertNotIn(DASHSCOPE_SECRET, combined)

    def test_qwen_only_rejects_dashscope_kimi_confirmation(self):
        with self.assertRaisesRegex(
            model_benchmark.BenchmarkError, "cannot be combined with --qwen-only",
        ):
            model_benchmark._validate_execution_mode(
                model_benchmark.DEFAULT_KIMI_ROUTE,
                True,
                False,
                True,
            )

    def test_global_budget_fails_before_output_or_paid_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "must-not-exist"
            moonshot = FakeClient()
            dashscope = FakeClient()
            with self.assertRaises(model_benchmark.BudgetExceeded):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"moonshot": moonshot, "dashscope": dashscope},
                    repeats=3,
                    ledger=model_benchmark.BudgetLedger(0.001),
                    output_path=output,
                )
            self.assertFalse(output.exists())
            self.assertEqual([], moonshot.calls)
            self.assertEqual([], dashscope.calls)

    def test_dashscope_moonshot_route_budget_fails_before_any_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "dashscope-route-budget-failure"
            dashscope = FakeClient()
            with self.assertRaisesRegex(
                model_benchmark.BenchmarkError,
                "paid execution is disabled",
            ):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"dashscope": dashscope}, repeats=3,
                    ledger=model_benchmark.BudgetLedger(0.001),
                    output_path=output,
                    kimi_route=model_benchmark.KIMI_ROUTE_DASHSCOPE_MOONSHOT,
                )
            self.assertFalse(output.exists())
            self.assertEqual([], dashscope.calls)

    def test_missing_usage_stops_after_first_paid_response(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "usage-failure"
            moonshot = MissingUsageClient()
            dashscope = FakeClient()
            with self.assertRaises(model_benchmark.UsageError):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"moonshot": moonshot, "dashscope": dashscope},
                    repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                )
            self.assertEqual(1, len(moonshot.calls))
            self.assertEqual([], dashscope.calls)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest["status"])

    def test_transport_failure_stops_before_any_other_provider_call(self):
        with tempfile.TemporaryDirectory() as tempdir:
            (
                evidence, evidence_path, evidence_file_hash,
                gold, artist_key, gold_hash,
            ) = self._load_inputs(tempdir)
            output = Path(tempdir) / "transport-failure"
            moonshot = TransportFailureClient()
            dashscope = FakeClient()
            with self.assertRaises(model_benchmark.ProviderError):
                model_benchmark.run_benchmark(
                    evidence, evidence_path, evidence_file_hash,
                    gold, gold_hash, artist_key,
                    {"moonshot": moonshot, "dashscope": dashscope},
                    repeats=3,
                    ledger=model_benchmark.BudgetLedger(20),
                    output_path=output,
                )
            self.assertEqual(1, len(moonshot.calls))
            self.assertEqual([], dashscope.calls)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", manifest["status"])

    def test_main_fails_closed_without_both_keys_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence_path = self._write_evidence(tempdir)
            gold_path = self._write_verified_gold(tempdir, self._evidence())
            output = Path(tempdir) / "no-keys-output"
            with mock.patch.dict(os.environ, {}, clear=True):
                code = model_benchmark.main([
                    "--evidence", str(evidence_path),
                    "--gold", str(gold_path),
                    "--output-dir", str(output),
                    "--repeats", "3",
                ])
            self.assertEqual(2, code)
            self.assertFalse(output.exists())

    def test_qwen_only_cli_requires_only_dashscope_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence_path = self._write_evidence(tempdir)
            gold_path = self._write_verified_gold(tempdir, self._evidence())
            output = Path(tempdir) / "qwen-only-cli-output"
            fake_scorecard = {"estimated_list_cost_cny": 0.1234}
            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(
                model_benchmark, "run_benchmark",
                return_value=(fake_scorecard, output),
            ) as run:
                code = model_benchmark.main([
                    "--evidence", str(evidence_path),
                    "--gold", str(gold_path),
                    "--output-dir", str(output),
                    "--repeats", "3",
                    "--qwen-only",
                ])
            self.assertEqual(0, code)
            self.assertTrue(run.call_args.kwargs["qwen_only"])
            self.assertEqual({"dashscope"}, set(run.call_args.args[6]))

    def test_dashscope_kimi_only_cli_requires_explicit_confirmation_and_one_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence_path = self._write_evidence(tempdir)
            gold_path = self._write_verified_gold(tempdir, self._evidence())
            output = Path(tempdir) / "dashscope-kimi-only-cli-output"
            fake_scorecard = {"estimated_list_cost_cny": 0.1234}
            args = [
                "--evidence", str(evidence_path),
                "--gold", str(gold_path),
                "--output-dir", str(output),
                "--repeats", "3",
                "--kimi-route", "dashscope-moonshot",
                "--dashscope-kimi-only",
            ]
            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(model_benchmark, "run_benchmark") as run:
                self.assertEqual(2, model_benchmark.main(args))
                run.assert_not_called()

            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(
                model_benchmark, "run_benchmark",
                return_value=(fake_scorecard, output),
            ) as run:
                code = model_benchmark.main([
                    *args,
                    model_benchmark.DASHSCOPE_KIMI_CONFIRMATION_FLAG,
                ])
            self.assertEqual(0, code)
            self.assertTrue(run.call_args.kwargs["dashscope_kimi_only"])
            self.assertTrue(
                run.call_args.kwargs[
                    "dashscope_kimi_unbounded_reasoning_account_cap_confirmed"
                ]
            )
            self.assertEqual({"dashscope"}, set(run.call_args.args[6]))

    def test_dashscope_aliyun_k3_only_cli_uses_exact_route_and_one_key(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence_path = self._write_evidence(tempdir)
            gold_path = self._write_verified_gold(tempdir, self._evidence())
            output = Path(tempdir) / "dashscope-aliyun-k3-cli-output"
            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(
                model_benchmark, "run_benchmark",
                return_value=({"estimated_list_cost_cny": 0.1234}, output),
            ) as run:
                code = model_benchmark.main([
                    "--evidence", str(evidence_path),
                    "--gold", str(gold_path),
                    "--output-dir", str(output),
                    "--repeats", "3",
                    "--kimi-route", "dashscope-aliyun-k3",
                    "--dashscope-kimi-only",
                    model_benchmark.DASHSCOPE_KIMI_CONFIRMATION_FLAG,
                ])
            self.assertEqual(0, code)
            self.assertEqual(
                model_benchmark.KIMI_ROUTE_DASHSCOPE_ALIYUN_K3,
                run.call_args.kwargs["kimi_route"],
            )
            self.assertTrue(run.call_args.kwargs["dashscope_kimi_only"])
            self.assertTrue(
                run.call_args.kwargs[
                    "dashscope_kimi_unbounded_reasoning_account_cap_confirmed"
                ]
            )
            self.assertEqual({"dashscope"}, set(run.call_args.args[6]))

    def test_qwen_only_rejects_nondefault_kimi_route_before_calls(self):
        args = model_benchmark.parse_args([
            "--evidence", "/tmp/not-read.json",
            "--qwen-only", "--kimi-route", "dashscope-moonshot",
        ])
        with self.assertRaisesRegex(
            model_benchmark.BenchmarkError, "cannot be combined",
        ):
            model_benchmark.model_specs_for_benchmark(
                args.kimi_route, args.qwen_only,
            )

    def test_cli_kimi_route_has_route_specific_key_requirements(self):
        with tempfile.TemporaryDirectory() as tempdir:
            evidence_path = self._write_evidence(tempdir)
            gold_path = self._write_verified_gold(tempdir, self._evidence())
            base_args = [
                "--evidence", str(evidence_path),
                "--gold", str(gold_path),
                "--output-dir", str(Path(tempdir) / "route-output"),
                "--repeats", "3",
            ]
            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(
                model_benchmark, "run_benchmark",
            ) as run:
                code = model_benchmark.main([
                    *base_args, "--kimi-route", "dashscope-moonshot",
                ])
                self.assertEqual(2, code)
                run.assert_not_called()

            with mock.patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": DASHSCOPE_SECRET}, clear=True,
            ), mock.patch.object(model_benchmark, "run_benchmark") as run:
                self.assertEqual(2, model_benchmark.main(base_args))
                run.assert_not_called()

            with mock.patch.dict(
                os.environ, {"MOONSHOT_API_KEY": MOONSHOT_SECRET}, clear=True,
            ), mock.patch.object(model_benchmark, "run_benchmark") as run:
                self.assertEqual(2, model_benchmark.main([
                    *base_args, "--kimi-route", "dashscope-moonshot",
                ]))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
