"""Sanitized, provider-reported usage telemetry for paid refresh calls.

The ledger intentionally stores counts, byte lengths and provider ``usage``
fields only.  Prompts, protected Formula outputs, model answers, reasoning text
and credentials must never be serialized into its artifacts.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PRICING_AS_OF = "2026-08-24"
PRICING_MAX_AGE_DAYS = 30
FORMULA_SEARCH_CNY_PER_CALL = 0.03

# Public China-region list prices in CNY per million tokens.  Unknown models
# still get exact usage telemetry, but no possibly-wrong cost projection.
MOONSHOT_PRICING_CNY_PER_MILLION = {
    "kimi-k3": {"cached_input": 2.0, "input": 20.0, "output": 100.0},
    "kimi-k2.7-code": {"cached_input": 1.3, "input": 6.5, "output": 27.0},
    "kimi-k2.6": {"cached_input": 1.1, "input": 6.5, "output": 27.0},
}


def _nonnegative_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, result)


def _utf8_bytes(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    return len(value.encode("utf-8"))


def _json_bytes(value: Any) -> int:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return 0
    return len(encoded)


def _response_mapping(response: Any) -> dict[str, Any]:
    """Return a safe mapping without trusting a provider's JSON root type."""
    return response if isinstance(response, dict) else {}


def _usage_from_response(response: Any) -> dict[str, int | None]:
    response = _response_mapping(response)
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}

    prompt_tokens = _nonnegative_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    completion_tokens = _nonnegative_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    total_tokens = _nonnegative_int(usage.get("total_tokens"))
    cached_tokens = _nonnegative_int(
        usage.get("cached_tokens", prompt_details.get("cached_tokens"))
    )
    reasoning_tokens = _nonnegative_int(
        usage.get("reasoning_tokens", completion_details.get("reasoning_tokens"))
    )
    if cached_tokens is not None and prompt_tokens is not None:
        cached_tokens = min(cached_tokens, prompt_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _message_lengths(response: Any) -> tuple[int, int]:
    response = _response_mapping(response)
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0, 0
    choice = choices[0]
    if not isinstance(choice, dict):
        return 0, 0
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        return 0, 0
    return (
        _utf8_bytes(message.get("content")),
        _utf8_bytes(message.get("reasoning_content")),
    )


def _round_cost(value: float) -> float:
    return round(value, 6)


class RefreshTelemetry:
    """Thread-safe ledger for one refresh, safe to publish as an artifact."""

    def __init__(self, model: str, context: dict[str, Any] | None = None) -> None:
        self.model = model
        self.context = dict(context or {})
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._started_monotonic = time.monotonic()
        self._lock = threading.Lock()
        self._sequence = 0
        self._formula_calls: list[dict[str, Any]] = []
        self._chat_calls: list[dict[str, Any]] = []

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def record_formula(
        self,
        *,
        artist_key: str,
        category: str,
        attempt: int,
        request_payload: dict[str, Any],
        response: Any,
        elapsed_seconds: float,
        outcome: str,
    ) -> None:
        response_map = _response_mapping(response)
        context = response_map.get("context") or {}
        if not isinstance(context, dict):
            context = {}
        output = context.get("output") or context.get("encrypted_output") or ""
        record = {
            "sequence": self._next_sequence(),
            "artist_key": artist_key,
            "category": category,
            "attempt": max(1, int(attempt)),
            "outcome": outcome,
            "fiber_id": str(response_map.get("id") or ""),
            "fiber_status": str(response_map.get("status") or ""),
            "request_bytes": _json_bytes(request_payload),
            "protected_output_bytes": _utf8_bytes(output),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
        }
        with self._lock:
            self._formula_calls.append(record)

    def record_chat(
        self,
        *,
        artist_key: str,
        attempt: int,
        request_payload: dict[str, Any],
        response: Any,
        elapsed_seconds: float,
        outcome: str,
    ) -> int:
        response_map = _response_mapping(response)
        usage = _usage_from_response(response_map)
        content_bytes, reasoning_bytes = _message_lengths(response_map)
        record = {
            "sequence": self._next_sequence(),
            "artist_key": artist_key,
            "attempt": max(1, int(attempt)),
            "outcome": outcome,
            "response_id": str(response_map.get("id") or ""),
            "model": str(response_map.get("model") or self.model),
            "request_bytes": _json_bytes(request_payload),
            "content_bytes": content_bytes,
            "reasoning_content_bytes": reasoning_bytes,
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
            **usage,
        }
        with self._lock:
            self._chat_calls.append(record)
        return int(record["sequence"])

    def mark_chat_validation(self, sequence: int, outcome: str) -> None:
        with self._lock:
            for record in self._chat_calls:
                if record["sequence"] == sequence:
                    record["validation"] = outcome
                    return

    def _summary_unlocked(self) -> dict[str, Any]:
        formula_calls = list(self._formula_calls)
        chat_calls = list(self._chat_calls)
        prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in chat_calls)
        cached_tokens = sum(int(item.get("cached_tokens") or 0) for item in chat_calls)
        completion_tokens = sum(
            int(item.get("completion_tokens") or 0) for item in chat_calls
        )
        reasoning_tokens = sum(
            int(item.get("reasoning_tokens") or 0) for item in chat_calls
        )
        usage_reported = sum(
            item.get("prompt_tokens") is not None
            and item.get("completion_tokens") is not None
            for item in chat_calls
        )
        china_pricing = self.context.get("pricing_region", "moonshot_cn") == "moonshot_cn"
        pricing = (
            MOONSHOT_PRICING_CNY_PER_MILLION.get(self.model)
            if china_pricing else None
        )
        chat_cost: float | None = None
        if pricing is not None and usage_reported == len(chat_calls):
            uncached_tokens = max(0, prompt_tokens - cached_tokens)
            chat_cost = (
                uncached_tokens * pricing["input"] / 1_000_000.0
                + cached_tokens * pricing["cached_input"] / 1_000_000.0
                + completion_tokens * pricing["output"] / 1_000_000.0
            )
        formula_cost = (
            len(formula_calls) * FORMULA_SEARCH_CNY_PER_CALL
            if china_pricing else None
        )
        total_cost = (
            None if chat_cost is None or formula_cost is None
            else formula_cost + chat_cost
        )
        return {
            "model": self.model,
            "pricing_as_of": PRICING_AS_OF,
            "pricing_max_age_days": PRICING_MAX_AGE_DAYS,
            "pricing_cny_per_million_tokens": (
                None if pricing is None else dict(pricing)
            ),
            "formula_calls": len(formula_calls),
            "formula_succeeded": sum(
                item.get("outcome") == "succeeded" for item in formula_calls
            ),
            "formula_cost_assumption_cny_per_call": FORMULA_SEARCH_CNY_PER_CALL,
            "formula_estimated_cost_cny": (
                None if formula_cost is None else _round_cost(formula_cost)
            ),
            "chat_calls": len(chat_calls),
            "chat_usage_reported_calls": usage_reported,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "uncached_prompt_tokens": max(0, prompt_tokens - cached_tokens),
            "completion_tokens_including_reasoning": completion_tokens,
            "reasoning_tokens_if_reported": reasoning_tokens,
            "chat_estimated_cost_cny": (
                None if chat_cost is None else _round_cost(chat_cost)
            ),
            "total_estimated_cost_cny": (
                None if total_cost is None else _round_cost(total_cost)
            ),
            "estimate_notes": [
                "Token totals come from provider response usage; K3 completion tokens include hidden reasoning.",
                "Formula cost counts every logical Fiber attempt at the published web-search unit price; reconcile with the provider bill.",
                "A null estimate means usage was incomplete, the model price is unknown, or the API endpoint is not the China pricing region.",
            ],
        }

    def _by_artist_unlocked(self) -> dict[str, dict[str, Any]]:
        keys = {
            str(item.get("artist_key") or "")
            for item in [*self._formula_calls, *self._chat_calls]
            if item.get("artist_key")
        }
        result: dict[str, dict[str, Any]] = {}
        china_pricing = self.context.get("pricing_region", "moonshot_cn") == "moonshot_cn"
        pricing = (
            MOONSHOT_PRICING_CNY_PER_MILLION.get(self.model)
            if china_pricing else None
        )
        for key in sorted(keys):
            formula = [item for item in self._formula_calls if item["artist_key"] == key]
            chat = [item for item in self._chat_calls if item["artist_key"] == key]
            prompt = sum(int(item.get("prompt_tokens") or 0) for item in chat)
            cached = sum(int(item.get("cached_tokens") or 0) for item in chat)
            completion = sum(int(item.get("completion_tokens") or 0) for item in chat)
            complete_usage = all(
                item.get("prompt_tokens") is not None
                and item.get("completion_tokens") is not None
                for item in chat
            )
            chat_cost: float | None = None
            if pricing is not None and complete_usage:
                chat_cost = (
                    max(0, prompt - cached) * pricing["input"] / 1_000_000.0
                    + cached * pricing["cached_input"] / 1_000_000.0
                    + completion * pricing["output"] / 1_000_000.0
                )
            formula_cost = (
                len(formula) * FORMULA_SEARCH_CNY_PER_CALL
                if china_pricing else None
            )
            result[key] = {
                "formula_calls": len(formula),
                "chat_calls": len(chat),
                "prompt_tokens": prompt,
                "cached_tokens": cached,
                "completion_tokens_including_reasoning": completion,
                "estimated_cost_cny": (
                    None if chat_cost is None or formula_cost is None
                    else _round_cost(formula_cost + chat_cost)
                ),
            }
        return result

    def snapshot(
        self, status: str = "running", error_type: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            summary = self._summary_unlocked()
            by_artist = self._by_artist_unlocked()
            formula_calls = sorted(self._formula_calls, key=lambda item: item["sequence"])
            chat_calls = sorted(self._chat_calls, key=lambda item: item["sequence"])
        return {
            "schema_version": 1,
            "status": status,
            "error_type": error_type,
            "started_at": self.started_at,
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(max(0.0, time.monotonic() - self._started_monotonic), 3),
            "context": dict(self.context),
            "summary": summary,
            "by_artist": by_artist,
            "formula_calls": formula_calls,
            "chat_calls": chat_calls,
        }

    def write(
        self, path: Path, status: str, error_type: str | None = None,
    ) -> None:
        resolved = path.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot(status=status, error_type=error_type)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=resolved.parent,
                prefix=".%s." % resolved.name, delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, resolved)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
