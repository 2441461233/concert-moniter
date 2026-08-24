#!/usr/bin/env python3
"""分层手动刷新：确定性采集/构建 + 可选 Kimi candidate。

确定性 ShowStart 采集永远先运行并构建可发布快照，不依赖模型 Key 或余额。
Kimi K3 与官方 ``moonshot/web-search:latest`` Formula 仅作为可选付费
candidate；所有已实测模型尚未通过质量门禁，因此 candidate 只能写到仓库外并
上传为 workflow artifact，永不自动进 inbox/合并生产数据。失败时保留旧
research 数据并显式标为 stale，不阻断确定性结果。

用法：
    python3 scripts/full_refresh.py
    MOONSHOT_API_KEY=... python3 scripts/full_refresh.py --enrich-provider kimi
    MOONSHOT_API_KEY=... python3 scripts/full_refresh.py \
        --enrich-provider kimi --research-only --output /tmp/research.json

可选环境变量：
    KIMI_RESEARCH_MODEL    默认 kimi-k3
    MOONSHOT_API_BASE      默认 https://api.moonshot.cn/v1
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import store  # noqa: E402
from lib.telemetry import (  # noqa: E402
    FORMULA_SEARCH_CNY_PER_CALL,
    MOONSHOT_PRICING_CNY_PER_MILLION,
    PRICING_AS_OF,
    PRICING_MAX_AGE_DAYS,
    RefreshTelemetry,
)
import monitor  # noqa: E402


DEFAULT_MODEL = "kimi-k3"
DEFAULT_WORKERS = 1
DEFAULT_TIMEOUT = 300
MAX_RETRIES = 3
MAX_HTTP_RETRIES = 5
MAX_COMPLETION_TOKENS = 16000
ENRICH_PROVIDERS = ("kimi", "none")
DEFAULT_ENRICH_PROVIDER = "none"
DEFAULT_ENRICH_GLOBAL_BUDGET_CNY = 5.0
DEFAULT_ENRICH_ARTIST_BUDGET_CNY = 5.0
DEFAULT_ENRICH_CONTEXT_BYTES = 128_000
DEFAULT_ENRICH_ATTEMPTS = 1
FORMULA_URI = "moonshot/web-search:latest"
FORMULA_TOOL_NAME = "web_search"
SHANGHAI_TZ = store.APP_TIMEZONE
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SOURCE_ID_RE = re.compile(r"^src_[a-f0-9]{16}$")
PRINT_LOCK = threading.Lock()
API_RATE_LOCK = threading.Lock()
LAST_API_REQUEST_AT = 0.0
SEARCH_CATEGORIES = ("ticketing", "official", "china_region", "rumors")

_BEARER_SECRET_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_NAMED_SECRET_RE = re.compile(
    r'''(?ix)
    (["']?(?:authorization|api[_-]?key|access[_-]?token|token|secret)["']?
     \s*[:=]\s*["']?)
    (?:bearer\s+)?[^\s,;"'}]+'''
)


class ResearchError(RuntimeError):
    """调研 API、联网来源或结果校验失败。"""


class QuotaError(ResearchError):
    """Moonshot 账户余额/额度不足，重试不会自愈。"""


class EnrichmentBudgetError(ResearchError):
    """The optional paid enrichment cannot fit its explicit hard budget."""


class EnrichmentContextError(ResearchError):
    """A finalizer request exceeds the configured byte-level context ceiling."""


EVENT_PROPERTIES = {
    "source_id": {"type": "string"},
    "tour_name": {"type": "string"},
    "title": {"type": "string"},
    "city": {"type": "string"},
    "country": {"type": "string"},
    "venue": {"type": "string"},
    "show_date": {"type": "string"},
    "doors_time": {"type": "string"},
    "show_time": {"type": "string"},
    "show_end_time": {"type": "string"},
    "curfew_time": {"type": "string"},
    "price": {"type": "string"},
    "ticket_tiers": {"type": "array", "items": {"type": "string"}},
    "sale_status": {
        "type": "string",
        "enum": [
            "on_sale", "upcoming", "sold_out", "ended",
            "cancelled", "postponed", "paused", "",
        ],
    },
    "sale_time": {"type": "string"},
    "confidence": {"type": "string", "enum": ["confirmed", "rumor"]},
    "note": {"type": "string"},
}

RUMOR_PROPERTIES = {
    "headline": {"type": "string"},
    "detail": {"type": "string"},
    "source_name": {"type": "string"},
    "source_id": {"type": "string"},
    "credibility": {"type": "string", "enum": ["high", "medium", "low"]},
    "posted_at": {"type": "string"},
}

ENRICH_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": EVENT_PROPERTIES,
                "required": list(EVENT_PROPERTIES),
            },
        },
        "rumors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": RUMOR_PROPERTIES,
                "required": list(RUMOR_PROPERTIES),
            },
        },
        "coverage": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ticketing_checked": {"type": "boolean"},
                "official_checked": {"type": "boolean"},
                "china_region_checked": {"type": "boolean"},
                "rumors_checked": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            "required": [
                "ticketing_checked",
                "official_checked",
                "china_region_checked",
                "rumors_checked",
                "summary",
            ],
        },
    },
    "required": ["events", "rumors", "coverage"],
}

# The isolated shadow harness predates the production source_id contract and
# imports RESULT_SCHEMA directly.  Keep that frozen comparison contract stable;
# paid production requests exclusively use ENRICH_RESULT_SCHEMA below.
LEGACY_EVENT_PROPERTIES = {
    "url": {"type": "string"},
    **{
        key: value for key, value in EVENT_PROPERTIES.items()
        if key not in {
            "source_id", "doors_time", "show_end_time", "curfew_time",
        }
    },
}
LEGACY_RUMOR_PROPERTIES = {
    **{key: value for key, value in RUMOR_PROPERTIES.items() if key != "source_id"},
    "url": {"type": "string"},
}
SOURCE_PROPERTIES = {
    "category": {"type": "string", "enum": list(SEARCH_CATEGORIES)},
    "title": {"type": "string"},
    "url": {"type": "string"},
}
RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": LEGACY_EVENT_PROPERTIES,
                "required": list(LEGACY_EVENT_PROPERTIES),
            },
        },
        "rumors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": LEGACY_RUMOR_PROPERTIES,
                "required": list(LEGACY_RUMOR_PROPERTIES),
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": SOURCE_PROPERTIES,
                "required": list(SOURCE_PROPERTIES),
            },
        },
        "coverage": ENRICH_RESULT_SCHEMA["properties"]["coverage"],
    },
    "required": ["events", "rumors", "sources", "coverage"],
}


def _redact_sensitive(value: Any) -> str:
    """Remove credentials even when a provider echoes them in an error body."""
    text = str(value)
    exact = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if exact:
        text = text.replace(exact, "[REDACTED]")
    text = _BEARER_SECRET_RE.sub("Bearer [REDACTED]", text)
    text = _NAMED_SECRET_RE.sub(r"\1[REDACTED]", text)
    return text


def _log(message: str) -> None:
    with PRINT_LOCK:
        print(_redact_sensitive(message), flush=True)


def _existing_context(artist: dict[str, Any]) -> dict[str, Any]:
    """把现有数据作为待复核线索，不当作事实直接复制。"""
    key = artist["key"]
    events = [
        value for value in store.load_events().values()
        if value.get("artist_key") == key and store.derive_status(value) != "ended"
    ]
    rumors = [
        value for value in store.load_rumors().values()
        if value.get("artist_key") == key
    ]
    return {"events": events[:40], "rumors": rumors[:25]}


def build_search_queries(artist: dict[str, Any], today: str) -> list[dict[str, str]]:
    """固定生成四类查询；覆盖事实由代码调用记录证明，而非模型自报。"""
    identity_parts = [artist["name"], *(artist.get("aliases") or [])]
    identity = " / ".join(dict.fromkeys(part.strip() for part in identity_parts if part.strip()))
    configured = " ".join(artist.get("search_terms") or [])
    year = int(today[:4])
    year_scope = "%d %d 未来" % (year, year + 1)
    is_kpop = artist.get("region") == "kpop"
    tour_scope = (
        "中国内地 香港 澳门 台湾 亚洲 世界巡演 韩文 英文 新增站 加场 补票 延期 取消"
        if is_kpop else
        "中国内地 香港 澳门 台湾 巡回演唱会 新增站 加场 补票 延期 取消"
    )
    return [
        {
            "category": "ticketing",
            "query": (
                f"{identity} {configured} {year_scope} 演唱会 开票时间 票价 场馆 "
                "大麦 秀动 票星球 猫眼 摩天轮 Cityline 拓元 Interpark NOL "
                "Ticketmaster Live Nation 正式票务"
            ).strip(),
        },
        {
            "category": "official",
            "query": (
                f"{identity} {year_scope} concert tour official 官方 公告 官网 事务所 "
                "Weverse 微博 X 主办方 场馆 fanclub presale 公售"
            ),
        },
        {
            "category": "china_region",
            "query": f"{identity} {year_scope} {tour_scope}",
        },
        {
            "category": "rumors",
            "query": (
                f"{identity} {year_scope} 演唱会 开票 近期 传闻 爆料 场馆档期 "
                "票务页面 行程 加场 rumor"
            ),
        },
    ]


def build_prompt(artist: dict[str, Any], today: str) -> str:
    aliases = ", ".join(artist.get("aliases") or [])
    existing = json.dumps(
        _existing_context(artist), ensure_ascii=False, separators=(",", ":"),
    )
    return f"""
今天是 {today}（Asia/Shanghai）。请根据紧随本消息之后的四组 Kimi 官方联网搜索结果，
对艺人 {artist['name']} 做一次完整、实时的演出与开票调研。

固定身份：
- artist_key: {artist['key']}
- region: {artist.get('region', '')}
- aliases: {aliases}

四个工具结果依次对应 ticketing、official、china_region、rumors。必须同时阅读四组结果：
1. ticketing：正式票务平台、演出日期、场馆、票价、先行及公售时间。
2. official：艺人/事务所、Weverse、官方微博/X、主办方和场馆公告。
3. china_region：中国内地及港澳台的新增站、加场、补票、延期、取消；KPop 还含完整亚洲/世巡。
4. rumors：只保留与未来演出/开票相关且仍可能变化的新线索。

输出规则：
- 重新整理当前全量有效信息，不是只找今天新增。
- 程序会在搜索结果后附上 PROGRAM_SOURCE_CATALOG。每条 event/rumor 只能填写其中
  一个固定 source_id；绝对不要生成、改写或输出 URL。程序会在校验通过后回填原 URL。
- 没有可用 source_id 就不要输出；未知、拼错或自行构造的 source_id 会让该艺人整份增强失败。
- 官方或正式票务可查才标 confirmed。论坛/搬运/曝光放 rumors。
- doors_time 只写明确开门时间，show_time 只写真正开演时间，show_end_time 只写明确
  演出结束时间，curfew_time 只写场馆宵禁/清场时间。不得把 doors/curfew 填进
  show_time/show_end_time；来源含义不清或彼此冲突时对应字段留空。
- 不猜日期、时间、价格或场馆；不确定的字段留空。时间只用 HH:MM，posted_at 必须为 YYYY-MM-DD。
- 不报无关的新歌/综艺/历史战绩，也不报已经结束的历史场次。
- coverage 四项只有在你确实阅读对应工具结果后才能为 true；查无结果也要在 summary 说明。

项目现有记录如下，只是本轮必须重新核实的候选线索，不能直接当作事实复制：
{existing}
""".strip()


def _moonshot_key() -> str:
    api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        raise ResearchError("缺少 MOONSHOT_API_KEY")
    return api_key


def _retry_delay(attempt: int, headers: Any = None) -> float:
    retry_after = headers.get("Retry-After") if headers is not None else None
    try:
        explicit = float(retry_after) if retry_after else 0.0
    except (TypeError, ValueError):
        explicit = 0.0
    return min(60.0, max(explicit, 5.0 * (2 ** attempt)))


def _pace_moonshot_request() -> None:
    """默认按 Tier 0 的 3 RPM 串行发起 Moonshot 请求。"""
    global LAST_API_REQUEST_AT
    raw_interval = os.environ.get("MOONSHOT_REQUEST_INTERVAL", "21")
    try:
        interval = max(0.0, float(raw_interval))
    except ValueError as exc:
        raise ResearchError("MOONSHOT_REQUEST_INTERVAL 必须是非负数字") from exc
    with API_RATE_LOCK:
        remaining = interval - (time.monotonic() - LAST_API_REQUEST_AT)
        if remaining > 0:
            time.sleep(remaining)
        LAST_API_REQUEST_AT = time.monotonic()


def _quota_exhausted(body: str) -> bool:
    normalized = body.lower()
    markers = (
        "insufficient_quota", "exceeded_current_quota", "insufficient balance", "quota exceeded",
        "account balance", "余额不足", "额度不足", "欠费",
    )
    return any(marker in normalized for marker in markers)


def _provider_error_code(body: str) -> str:
    """Extract only a non-sensitive provider code/type, never its free text."""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return "unclassified_provider_error"
    if not isinstance(parsed, dict):
        return "unclassified_provider_error"
    error = parsed.get("error")
    if not isinstance(error, dict):
        error = parsed
    for key in ("code", "type"):
        raw = str(error.get(key) or "").strip()
        if raw and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", raw):
            return raw
    return "unclassified_provider_error"


def _moonshot_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int = MAX_HTTP_RETRIES,
) -> dict[str, Any]:
    base = os.environ.get("MOONSHOT_API_BASE", "https://api.moonshot.cn/v1").rstrip("/")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={
            "Authorization": "Bearer " + _moonshot_key(),
            "Content-Type": "application/json",
            "User-Agent": "concert-monitor-full-refresh/2.0",
        },
        method=method,
    )
    last_error: Exception | None = None
    max_attempts = max(1, int(max_attempts))
    for attempt in range(max_attempts):
        try:
            _pace_moonshot_request()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:1200]
            error_code = _provider_error_code(body)
            last_error = ResearchError(
                "Kimi HTTP %s: %s" % (exc.code, error_code)
            )
            if exc.code == 429 and _quota_exhausted(body):
                raise QuotaError(
                    "Kimi HTTP 429: quota_or_balance_unavailable"
                ) from exc
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise last_error from exc
            if attempt + 1 < max_attempts:
                delay = _retry_delay(attempt, exc.headers)
                _log("  ! Kimi HTTP %s，%.0f 秒后重试" % (exc.code, delay))
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = ResearchError(
                "Kimi 请求失败: %s" % _redact_sensitive(exc)
            )
            if attempt + 1 < max_attempts:
                delay = _retry_delay(attempt)
                _log("  ! Kimi 网络请求失败，%.0f 秒后重试" % delay)
                time.sleep(delay)
    raise last_error or ResearchError("Kimi 请求失败")


def load_formula_tools() -> list[dict[str, Any]]:
    response = _moonshot_request("GET", "/formulas/%s/tools" % FORMULA_URI)
    tools = response.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ResearchError("Kimi Formula 未返回工具定义")
    names = {
        tool.get("function", {}).get("name")
        for tool in tools if isinstance(tool, dict)
    }
    if FORMULA_TOOL_NAME not in names:
        raise ResearchError("Kimi Formula 工具定义缺少 web_search")
    return tools


def call_formula_api(payload: dict[str, Any]) -> dict[str, Any]:
    return _moonshot_request(
        "POST", "/formulas/%s/fibers" % FORMULA_URI, payload,
    )


def call_chat_api(payload: dict[str, Any]) -> dict[str, Any]:
    return _moonshot_request("POST", "/chat/completions", payload)


def call_formula_api_once(payload: dict[str, Any]) -> dict[str, Any]:
    """One transport send: optional enrichment must never retry an unknown charge."""
    return _moonshot_request(
        "POST", "/formulas/%s/fibers" % FORMULA_URI, payload, max_attempts=1,
    )


def call_chat_api_once(payload: dict[str, Any]) -> dict[str, Any]:
    """One transport send: deterministic publishing survives any enrich failure."""
    return _moonshot_request(
        "POST", "/chat/completions", payload, max_attempts=1,
    )


FormulaRequester = Callable[[dict[str, Any]], Any]
ChatRequester = Callable[[dict[str, Any]], Any]
URLChecker = Callable[[str], bool]


def _formula_output(fiber: Any) -> str:
    if not isinstance(fiber, dict):
        raise ResearchError("Kimi Formula 响应顶层必须是 object")
    if fiber.get("status") != "succeeded":
        raise ResearchError("Kimi Formula 执行失败: %s" % (
            fiber.get("error") or fiber.get("status") or "unknown",
        ))
    context = fiber.get("context") or {}
    output = context.get("output") or context.get("encrypted_output") or ""
    if not isinstance(output, str) or not output.strip():
        raise ResearchError("Kimi Formula 搜索没有返回上下文")
    return output


def _source_id_for_url(url: str) -> str:
    """Return a stable opaque ID; the model never controls the underlying URL."""
    identity = _url_identity(url)
    if not identity[0]:
        raise ResearchError("cannot assign source_id to an invalid URL")
    canonical = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return "src_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _extract_formula_sources(
    fiber: Any, category: str,
) -> list[dict[str, str]]:
    """Extract provider-owned reference URLs before the model can rewrite them.

    Formula search output may be encrypted for model consumption.  Enrichment is
    accepted only when the provider also returns an enumerable reference catalog.
    This intentionally fails closed during migration instead of trusting a URL
    copied or reconstructed by the language model.
    """
    if not isinstance(fiber, dict):
        return []
    context = fiber.get("context") if isinstance(fiber.get("context"), dict) else {}
    containers: list[Any] = []
    for owner in (fiber, context):
        for key in ("references", "sources", "citations", "search_results"):
            value = owner.get(key)
            if isinstance(value, list):
                containers.extend(value)
            elif isinstance(value, dict):
                for nested_key in ("items", "results", "data"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        containers.extend(nested)

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in containers:
        if isinstance(raw, str):
            url, title = raw.strip(), ""
        elif isinstance(raw, dict):
            url = str(
                raw.get("url") or raw.get("link") or raw.get("source_url") or ""
            ).strip()
            title = str(raw.get("title") or raw.get("name") or "").strip()
        else:
            continue
        if not _public_http_url(url, resolve=False):
            continue
        source_id = _source_id_for_url(url)
        if source_id in seen:
            continue
        seen.add(source_id)
        sources.append({
            "source_id": source_id,
            "category": category,
            "title": title,
            "url": url,
        })
    return sources


def _source_catalog(searches: list[dict[str, Any]]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    for search in searches:
        for source in search.get("source_catalog") or []:
            source_id = str(source.get("source_id") or "")
            if source_id in seen:
                continue
            seen.add(source_id)
            catalog.append(source)
    return catalog


def execute_searches(
    artist: dict[str, Any],
    today: str,
    requester: FormulaRequester = call_formula_api,
    telemetry: RefreshTelemetry | None = None,
    max_attempts: int = MAX_RETRIES,
) -> list[dict[str, Any]]:
    max_attempts = int(max_attempts)
    if max_attempts < 1:
        raise ResearchError("Formula max_attempts must be at least 1")
    executions: list[dict[str, Any]] = []
    for index, item in enumerate(build_search_queries(artist, today)):
        arguments = json.dumps(
            {"query": item["query"]}, ensure_ascii=False, separators=(",", ":"),
        )
        body = {"name": FORMULA_TOOL_NAME, "arguments": arguments}
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            started_at = time.monotonic()
            fiber: dict[str, Any] | None = None
            try:
                fiber = requester(body)
                output = _formula_output(fiber)
                if telemetry is not None:
                    telemetry.record_formula(
                        artist_key=artist["key"], category=item["category"],
                        attempt=attempt, request_payload=body, response=fiber,
                        elapsed_seconds=time.monotonic() - started_at,
                        outcome="succeeded",
                    )
                executions.append({
                    **item,
                    "tool_call_id": "%s:%d" % (FORMULA_TOOL_NAME, index),
                    "fiber_id": str(fiber.get("id") or ""),
                    "output": output,
                    "source_catalog": _extract_formula_sources(
                        fiber, item["category"],
                    ),
                })
                break
            except QuotaError:
                if telemetry is not None:
                    telemetry.record_formula(
                        artist_key=artist["key"], category=item["category"],
                        attempt=attempt, request_payload=body, response=fiber,
                        elapsed_seconds=time.monotonic() - started_at,
                        outcome="quota_error",
                    )
                raise
            except (ResearchError, OSError) as exc:
                if telemetry is not None:
                    telemetry.record_formula(
                        artist_key=artist["key"], category=item["category"],
                        attempt=attempt, request_payload=body, response=fiber,
                        elapsed_seconds=time.monotonic() - started_at,
                        outcome="failed",
                    )
                last_error = exc
                if attempt < max_attempts:
                    delay = 2 ** (attempt - 1)
                    _log("  ! %-14s %s 搜索失败，%d 秒后重试：%s" % (
                        artist["name"], item["category"], delay, exc,
                    ))
                    time.sleep(delay)
        else:
            raise ResearchError("%s 搜索连续 %d 次失败: %s" % (
                item["category"], max_attempts, last_error,
            ))
    completed = {item["category"] for item in executions}
    if completed != set(SEARCH_CATEGORIES):
        raise ResearchError("四类 Kimi Formula 搜索未全部执行")
    return executions


def build_request(
    artist: dict[str, Any],
    model: str,
    today: str,
    tools: list[dict[str, Any]],
    searches: list[dict[str, Any]],
) -> dict[str, Any]:
    catalog = _source_catalog(searches)
    if not catalog:
        raise ResearchError(
            "Formula response has no enumerable provider reference catalog; "
            "source_id enrichment is blocked"
        )
    tool_calls = [{
        "id": item["tool_call_id"],
        "type": "function",
        "function": {
            "name": FORMULA_TOOL_NAME,
            "arguments": json.dumps(
                {"query": item["query"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    } for item in searches]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是严谨的演出信息研究员。只依据提供的 Kimi 官方搜索工具结果，"
                "不要使用记忆补写事实。"
            ),
        },
        {"role": "user", "content": build_prompt(artist, today)},
        {"role": "assistant", "content": None, "tool_calls": tool_calls},
    ]
    messages.extend({
        "role": "tool",
        "tool_call_id": item["tool_call_id"],
        "content": item["output"],
    } for item in searches)
    messages.append({
        "role": "user",
        "content": (
            "PROGRAM_SOURCE_CATALOG（程序生成；输出只能引用 source_id，禁止输出 URL）：\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        ),
    })
    return {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "none",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "concert_research",
                "strict": True,
                "schema": ENRICH_RESULT_SCHEMA,
            },
        },
        "reasoning_effort": "high",
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }


def _payload_utf8_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))


def validate_enrichment_context(
    payload: dict[str, Any], max_context_bytes: int | None,
) -> int:
    """Use full request bytes as a conservative, tokenizer-independent ceiling."""
    size = _payload_utf8_bytes(payload)
    if max_context_bytes is not None and size > max_context_bytes:
        raise EnrichmentContextError(
            "Kimi enrich payload %d bytes exceeds --enrich-max-context-bytes %d"
            % (size, max_context_bytes)
        )
    return size


def preflight_enrichment_budget(
    *, artist_count: int, model: str, global_limit_cny: float,
    per_artist_limit_cny: float, max_context_bytes: int, attempts: int,
    pricing_today: str | None = None,
) -> dict[str, float | int]:
    """Reserve the full configured envelope before the first paid Formula call.

    The input estimate treats every UTF-8 byte as a possible token and assumes no
    cache hit. Application-level paid retries are bounded by ``attempts``; normal
    production enrichment uses one attempt and one HTTP send per paid request.
    """
    values = (global_limit_cny, per_artist_limit_cny)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise EnrichmentBudgetError("enrichment budgets must be positive finite numbers")
    if artist_count < 1 or max_context_bytes < 1 or attempts < 1:
        raise EnrichmentBudgetError("invalid enrichment preflight limits")
    try:
        price_date = datetime.strptime(PRICING_AS_OF, "%Y-%m-%d").date()
        check_date = (
            datetime.strptime(pricing_today, "%Y-%m-%d").date()
            if pricing_today is not None
            else datetime.now(SHANGHAI_TZ).date()
        )
    except (TypeError, ValueError) as exc:
        raise EnrichmentBudgetError("invalid pinned pricing date") from exc
    price_age_days = (check_date - price_date).days
    if price_age_days < 0 or price_age_days > PRICING_MAX_AGE_DAYS:
        raise EnrichmentBudgetError(
            "pinned list price from %s is outside the %d-day freshness window; "
            "optional paid enrichment is blocked"
            % (PRICING_AS_OF, PRICING_MAX_AGE_DAYS)
        )
    pricing = MOONSHOT_PRICING_CNY_PER_MILLION.get(model)
    if pricing is None:
        raise EnrichmentBudgetError(
            "no pinned China list price for model %s; optional enrichment is blocked" % model
        )
    formula_reserve = (
        len(SEARCH_CATEGORIES) * attempts * FORMULA_SEARCH_CNY_PER_CALL
    )
    chat_reserve = attempts * (
        (max_context_bytes + 4096) * pricing["input"] / 1_000_000.0
        + MAX_COMPLETION_TOKENS * pricing["output"] / 1_000_000.0
    )
    per_artist_reserve = formula_reserve + chat_reserve
    total_reserve = artist_count * per_artist_reserve
    if per_artist_reserve > per_artist_limit_cny + 1e-9:
        raise EnrichmentBudgetError(
            "per-artist worst-case reserve ¥%.4f exceeds limit ¥%.4f"
            % (per_artist_reserve, per_artist_limit_cny)
        )
    if total_reserve > global_limit_cny + 1e-9:
        raise EnrichmentBudgetError(
            "all-artist worst-case reserve ¥%.4f exceeds global limit ¥%.4f"
            % (total_reserve, global_limit_cny)
        )
    return {
        "artist_count": artist_count,
        "attempts_per_paid_stage": attempts,
        "max_context_bytes": max_context_bytes,
        "pricing_as_of": PRICING_AS_OF,
        "pricing_age_days": price_age_days,
        "pricing_max_age_days": PRICING_MAX_AGE_DAYS,
        "per_artist_reserved_cny": round(per_artist_reserve, 6),
        "global_reserved_cny": round(total_reserve, 6),
    }


def _output_text(response: Any) -> str:
    if not isinstance(response, dict):
        raise ResearchError("Kimi 响应顶层必须是 object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ResearchError("Kimi 响应没有 choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ResearchError("Kimi 响应 choice 必须是 object")
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ResearchError("Kimi 响应 message 必须是 object")
    if message.get("tool_calls"):
        raise ResearchError("Kimi 汇总阶段意外请求了额外工具")
    if message.get("refusal"):
        raise ResearchError("Kimi 拒绝: %s" % message["refusal"])
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ResearchError("Kimi 响应没有结构化文本")
    if choice.get("finish_reason") not in (None, "stop"):
        raise ResearchError("Kimi 响应未完成: %s" % choice.get("finish_reason"))
    return content


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "result") -> None:
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
    }.get(expected, True)
    if not valid_type:
        raise ResearchError("%s 类型应为 %s" % (path, expected))
    if "enum" in schema and value not in schema["enum"]:
        raise ResearchError("%s 值不在允许范围" % path)
    if expected == "object":
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            raise ResearchError("%s 缺少字段: %s" % (path, ", ".join(missing)))
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extras = [key for key in value if key not in properties]
            if extras:
                raise ResearchError("%s 包含多余字段: %s" % (path, ", ".join(extras)))
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], "%s.%s" % (path, key))
    elif expected == "array":
        item_schema = schema.get("items") or {}
        for index, item in enumerate(value):
            _validate_schema(item, item_schema, "%s[%d]" % (path, index))


def _valid_calendar_date(value: str, allow_time: bool = False) -> bool:
    if not value:
        return True
    if allow_time and DATE_TIME_RE.fullmatch(value):
        formats = ["%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"]
    elif not allow_time and DATE_RE.fullmatch(value):
        formats = ["%Y-%m-%d"]
    else:
        return False
    return any(_can_parse_datetime(value, fmt) for fmt in formats)


def _can_parse_datetime(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _url_identity(url: str) -> tuple[str, str, int | None, str, str]:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except (ValueError, AttributeError):
        return "", "", None, "", ""
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    return scheme, host, port, path, parsed.query


def _public_http_url(url: str, resolve: bool = False) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except (ValueError, AttributeError):
        return False
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password or port not in (None, 80, 443):
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal")):
        return False
    # Reject literal/local-looking hosts.  Do not resolve or fetch provider URLs:
    # DNS preflight followed by urllib would be vulnerable to DNS rebinding.
    if re.fullmatch(r"\d+(?:\.\d+){3}", host) or ":" in host:
        return False
    return "." in host


def source_url_candidate_safe(url: str) -> bool:
    """Passive syntax gate; reachability and field support remain manual review."""
    return _public_http_url(url, resolve=False)


def _validate_source_id_result(
    artist: dict[str, Any],
    response: dict[str, Any],
    searches: list[dict[str, Any]],
    url_checker: URLChecker = source_url_candidate_safe,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    try:
        result = json.loads(_output_text(response))
    except json.JSONDecodeError as exc:
        raise ResearchError("Kimi 结构化输出不是 JSON: %s" % exc) from exc
    _validate_schema(result, ENRICH_RESULT_SCHEMA)

    executed = {item["category"] for item in searches if item.get("output")}
    if executed != set(SEARCH_CATEGORIES):
        raise ResearchError("代码没有完成四类 Formula 搜索")
    coverage = result["coverage"]
    coverage_keys = (
        "ticketing_checked", "official_checked",
        "china_region_checked", "rumors_checked",
    )
    missing = [key for key in coverage_keys if coverage.get(key) is not True]
    if missing:
        raise ResearchError("Kimi 搜索覆盖声明不完整: " + ", ".join(missing))

    warnings: list[str] = []
    catalog = _source_catalog(searches)
    if not catalog:
        raise ResearchError("Formula provider reference catalog is empty")
    catalog_by_id: dict[str, dict[str, str]] = {}
    for raw in catalog:
        source_id = str(raw.get("source_id") or "")
        url = str(raw.get("url") or "")
        if (
            not SOURCE_ID_RE.fullmatch(source_id)
            or not _public_http_url(url, resolve=False)
            or source_id != _source_id_for_url(url)
        ):
            raise ResearchError("program source catalog failed integrity validation")
        existing = catalog_by_id.get(source_id)
        if existing is not None and _url_identity(existing["url"]) != _url_identity(url):
            raise ResearchError("source_id collision in program source catalog")
        catalog_by_id[source_id] = raw

    referenced_ids = list(dict.fromkeys(
        str(item.get("source_id") or "")
        for item in [*result["events"], *result["rumors"]]
    ))
    unknown_ids = [
        source_id for source_id in referenced_ids
        if source_id not in catalog_by_id
    ]
    if unknown_ids:
        raise ResearchError(
            "model cited unknown program source_id: " + ", ".join(unknown_ids[:5])
        )
    if len(referenced_ids) > 40:
        raise ResearchError("model cited more than 40 program sources")

    source_candidates = [catalog_by_id[source_id] for source_id in referenced_ids]
    candidate_safety: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(source_candidates) or 1),
    ) as executor:
        future_map = {
            executor.submit(url_checker, raw["url"]): raw
            for raw in source_candidates
        }
        for future in concurrent.futures.as_completed(future_map):
            raw = future_map[future]
            try:
                candidate_safety[raw["source_id"]] = bool(future.result())
            except Exception:
                candidate_safety[raw["source_id"]] = False
    unsafe_ids = [
        raw["source_id"] for raw in source_candidates
        if not candidate_safety.get(raw["source_id"])
    ]
    if unsafe_ids:
        raise ResearchError(
            "program source URL is unsafe or invalid: " + ", ".join(unsafe_ids[:5])
        )
    sources = list(source_candidates)

    events: list[dict[str, Any]] = []
    for index, raw in enumerate(result["events"]):
        if not raw["title"].strip():
            raise ResearchError("event[%d] 缺少 title" % index)
        source_id = raw["source_id"]
        if not candidate_safety.get(source_id):
            raise ResearchError("event[%d] 的固定来源不安全或无效" % index)
        if (
            raw.get("confidence") == "confirmed"
            and catalog_by_id[source_id].get("category") == "rumors"
        ):
            raise ResearchError(
                "event[%d] confirmed 不得只引用 rumors 类来源" % index
            )
        if not _valid_calendar_date(raw["show_date"]):
            raise ResearchError("event[%d] show_date 无效" % index)
        if not _valid_calendar_date(raw["sale_time"], allow_time=True):
            raise ResearchError("event[%d] sale_time 无效" % index)
        invalid_times = [
            field for field in (
                "doors_time", "show_time", "show_end_time", "curfew_time",
            )
            if raw[field] and not TIME_RE.fullmatch(raw[field])
        ]
        if invalid_times:
            raise ResearchError(
                "event[%d] %s 不是明确 HH:MM"
                % (index, "/".join(invalid_times))
            )
        events.append({
            "source": "research",
            "artist_key": artist["key"],
            "artist_name": artist["name"],
            **raw,
            "url": catalog_by_id[source_id]["url"],
        })

    rumors: list[dict[str, Any]] = []
    for index, raw in enumerate(result["rumors"]):
        if not raw["headline"].strip():
            raise ResearchError("rumor[%d] 缺少 headline" % index)
        source_id = raw["source_id"]
        if not candidate_safety.get(source_id):
            raise ResearchError("rumor[%d] 的固定来源不安全或无效" % index)
        if not raw["posted_at"] or not _valid_calendar_date(raw["posted_at"]):
            raise ResearchError(
                "rumor[%d] posted_at 不精确到有效日期" % index
            )
        rumors.append({
            "artist_key": artist["key"],
            "artist_name": artist["name"],
            **raw,
            "url": catalog_by_id[source_id]["url"],
        })

    return {
        "events": events,
        "rumors": rumors,
        "coverage": coverage,
    }, sources, warnings


def _validate_legacy_shadow_result(
    artist: dict[str, Any], response: dict[str, Any],
    searches: list[dict[str, Any]], url_checker: URLChecker,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Compatibility validator for the frozen, repository-external shadow tool."""
    try:
        result = json.loads(_output_text(response))
    except json.JSONDecodeError as exc:
        raise ResearchError("Kimi 结构化输出不是 JSON: %s" % exc) from exc
    _validate_schema(result, RESULT_SCHEMA)
    executed = {item["category"] for item in searches if item.get("output")}
    if executed != set(SEARCH_CATEGORIES):
        raise ResearchError("代码没有完成四类 Formula 搜索")
    coverage = result["coverage"]
    coverage_keys = (
        "ticketing_checked", "official_checked",
        "china_region_checked", "rumors_checked",
    )
    missing = [key for key in coverage_keys if coverage.get(key) is not True]
    if missing:
        raise ResearchError("Kimi 搜索覆盖声明不完整: " + ", ".join(missing))

    warnings: list[str] = []
    referenced_urls = {
        _url_identity(item["url"])
        for item in [*result["events"], *result["rumors"]]
        if _public_http_url(item["url"], resolve=False)
    }
    candidates: list[
        tuple[int, dict[str, str], tuple[str, str, int | None, str, str]]
    ] = []
    seen: set[tuple[str, str, int | None, str, str]] = set()
    for index, raw in enumerate(result["sources"]):
        identity = _url_identity(raw["url"])
        if not identity[0] or not _public_http_url(raw["url"], resolve=False):
            warnings.append("source[%d] 不是安全的公开 HTTP(S) URL，已丢弃" % index)
            continue
        if identity in seen or identity not in referenced_urls:
            continue
        seen.add(identity)
        if len(candidates) < 40:
            candidates.append((index, raw, identity))
    if len(seen) > 40:
        warnings.append("本轮实际引用来源超过 40 条，仅校验并保留前 40 条")

    reachability: dict[tuple[str, str, int | None, str, str], bool] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(candidates) or 1),
    ) as executor:
        future_map = {
            executor.submit(url_checker, raw["url"]): (index, raw, identity)
            for index, raw, identity in candidates
        }
        for future in concurrent.futures.as_completed(future_map):
            index, _, identity = future_map[future]
            try:
                reachability[identity] = bool(future.result())
            except Exception:
                reachability[identity] = False
            if not reachability[identity]:
                warnings.append("source[%d] URL 无法访问，已丢弃" % index)
    sources = [
        raw for _, raw, identity in candidates if reachability.get(identity)
    ]
    source_urls = {_url_identity(item["url"]) for item in sources}

    events: list[dict[str, Any]] = []
    for index, raw in enumerate(result["events"]):
        if not raw["title"].strip():
            warnings.append("event[%d] 缺少 title，已丢弃" % index)
            continue
        if _url_identity(raw["url"]) not in source_urls:
            warnings.append("event[%d] URL 未匹配本轮可达来源，已丢弃" % index)
            continue
        if not _valid_calendar_date(raw["show_date"]):
            warnings.append("event[%d] show_date 无效，已丢弃" % index)
            continue
        if not _valid_calendar_date(raw["sale_time"], allow_time=True):
            warnings.append("event[%d] sale_time 无效，已丢弃" % index)
            continue
        events.append({
            "source": "research",
            "artist_key": artist["key"],
            "artist_name": artist["name"],
            **raw,
        })

    rumors: list[dict[str, Any]] = []
    for index, raw in enumerate(result["rumors"]):
        if not raw["headline"].strip():
            warnings.append("rumor[%d] 缺少 headline，已丢弃" % index)
            continue
        if _url_identity(raw["url"]) not in source_urls:
            warnings.append("rumor[%d] URL 未匹配本轮可达来源，已丢弃" % index)
            continue
        if not raw["posted_at"] or not _valid_calendar_date(raw["posted_at"]):
            warnings.append("rumor[%d] posted_at 不精确到有效日期，已丢弃" % index)
            continue
        rumors.append({
            "artist_key": artist["key"],
            "artist_name": artist["name"],
            **raw,
        })
    return {
        "events": events,
        "rumors": rumors,
        "coverage": coverage,
    }, sources, warnings


def _validate_result(
    artist: dict[str, Any], response: dict[str, Any],
    searches: list[dict[str, Any]],
    url_checker: URLChecker = source_url_candidate_safe,
    *, contract: str = "legacy_shadow",
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    """Validate either the frozen shadow contract or explicit production v2.

    Production callers must opt into ``source_id_v2``.  Keeping the old default
    is solely for the isolated shadow harness, whose schema is frozen for fair
    historical comparisons and never enters the production inbox.
    """
    if contract == "source_id_v2":
        return _validate_source_id_result(
            artist, response, searches, url_checker,
        )
    if contract == "legacy_shadow":
        return _validate_legacy_shadow_result(
            artist, response, searches, url_checker,
        )
    raise ResearchError("unknown result validation contract")


def research_artist(
    artist: dict[str, Any],
    model: str,
    today: str,
    tools: list[dict[str, Any]],
    requester: ChatRequester = call_chat_api,
    search_requester: FormulaRequester = call_formula_api,
    url_checker: URLChecker = source_url_candidate_safe,
    retries: int = MAX_RETRIES,
    telemetry: RefreshTelemetry | None = None,
    search_retries: int = MAX_RETRIES,
    max_context_bytes: int | None = None,
) -> dict[str, Any]:
    retries = int(retries)
    if retries < 1:
        raise ResearchError("Chat retries must be at least 1")
    searches = execute_searches(
        artist, today, search_requester, telemetry,
        max_attempts=search_retries,
    )
    payload = build_request(artist, model, today, tools, searches)
    validate_enrichment_context(payload, max_context_bytes)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        started_at = time.monotonic()
        response: Any = None
        telemetry_sequence: int | None = None
        try:
            response = requester(payload)
            if telemetry is not None:
                telemetry_sequence = telemetry.record_chat(
                    artist_key=artist["key"], attempt=attempt,
                    request_payload=payload, response=response,
                    elapsed_seconds=time.monotonic() - started_at,
                    outcome="response_received",
                )
            result, sources, warnings = _validate_result(
                artist, response, searches, url_checker,
                contract="source_id_v2",
            )
            if telemetry is not None and telemetry_sequence is not None:
                telemetry.mark_chat_validation(telemetry_sequence, "passed")
            archive_searches = [{
                "category": item["category"],
                "query": item["query"],
                "fiber_id": item["fiber_id"],
            } for item in searches]
            return {
                "artist": artist,
                **result,
                "sources": sources,
                "searches": archive_searches,
                "warnings": warnings,
            }
        except QuotaError:
            if telemetry is not None and telemetry_sequence is None:
                telemetry.record_chat(
                    artist_key=artist["key"], attempt=attempt,
                    request_payload=payload, response=response,
                    elapsed_seconds=time.monotonic() - started_at,
                    outcome="quota_error",
                )
            raise
        except (ResearchError, OSError) as exc:
            if telemetry is not None:
                if telemetry_sequence is None:
                    telemetry.record_chat(
                        artist_key=artist["key"], attempt=attempt,
                        request_payload=payload, response=response,
                        elapsed_seconds=time.monotonic() - started_at,
                        outcome="request_failed",
                    )
                else:
                    telemetry.mark_chat_validation(
                        telemetry_sequence, "failed:%s" % type(exc).__name__,
                    )
            last_error = exc
            if attempt < retries:
                delay = 2 ** (attempt - 1)
                _log("  ! %-14s Kimi 汇总第 %d 次失败，%d 秒后重试：%s" % (
                    artist["name"], attempt, delay, exc,
                ))
                time.sleep(delay)
    raise ResearchError("%s Kimi 汇总连续 %d 次失败: %s" % (
        artist["name"], retries, last_error,
    ))


def research_all(
    artists: list[dict[str, Any]],
    model: str,
    workers: int = DEFAULT_WORKERS,
    requester: ChatRequester = call_chat_api,
    search_requester: FormulaRequester = call_formula_api,
    tools: list[dict[str, Any]] | None = None,
    url_checker: URLChecker = source_url_candidate_safe,
    telemetry: RefreshTelemetry | None = None,
    chat_retries: int = MAX_RETRIES,
    search_retries: int = MAX_RETRIES,
    max_context_bytes: int | None = None,
    fail_fast: bool = False,
) -> dict[str, Any]:
    started = datetime.now(SHANGHAI_TZ)
    today = started.strftime("%Y-%m-%d")
    formula_tools = tools if tools is not None else load_formula_tools()
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    _log("开始 Kimi 全量联网调研：%d 位艺人 / 模型 %s" % (len(artists), model))

    max_workers = max(1, min(workers, len(artists) or 1))

    def record_result(artist: dict[str, Any], value: dict[str, Any]) -> None:
        results[artist["key"]] = value
        _log("  · %-14s 演出 %d / 舆情 %d / 来源 %d" % (
            artist["name"], len(value["events"]), len(value["rumors"]),
            len(value["sources"]),
        ))

    # 生产默认单路：余额耗尽时必须立即停止，不应先把其余
    # 艺人都提交进 executor，让每个任务再白请求一次。
    if max_workers == 1:
        for artist in artists:
            try:
                value = research_artist(
                    artist, model, today, formula_tools,
                    requester, search_requester, url_checker,
                    telemetry=telemetry,
                    retries=chat_retries,
                    search_retries=search_retries,
                    max_context_bytes=max_context_bytes,
                )
                record_result(artist, value)
            except QuotaError:
                raise
            except Exception as exc:
                if fail_fast:
                    raise ResearchError(
                        "%s enrichment failed; remaining artists were not called: %s"
                        % (artist["name"], exc)
                    ) from exc
                failures.append("%s: %s" % (artist["name"], exc))
                _log("  ! %-14s 失败：%s" % (artist["name"], exc))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    research_artist, artist, model, today, formula_tools,
                    requester, search_requester, url_checker,
                    telemetry=telemetry,
                    retries=chat_retries,
                    search_retries=search_retries,
                    max_context_bytes=max_context_bytes,
                ): artist
                for artist in artists
            }
            for future in concurrent.futures.as_completed(future_map):
                artist = future_map[future]
                try:
                    value = future.result()
                    record_result(artist, value)
                except QuotaError:
                    for pending in future_map:
                        pending.cancel()
                    raise
                except Exception as exc:  # 等全部单元结束后给出完整失败清单
                    if fail_fast:
                        for pending in future_map:
                            pending.cancel()
                        raise ResearchError(
                            "%s enrichment failed; pending artists were cancelled: %s"
                            % (artist["name"], exc)
                        ) from exc
                    failures.append("%s: %s" % (artist["name"], exc))
                    _log("  ! %-14s 失败：%s" % (artist["name"], exc))

    if failures:
        raise ResearchError("全量刷新未覆盖全员，未写入数据：\n" + "\n".join(failures))

    ordered = [results[artist["key"]] for artist in artists]
    events = [event for item in ordered for event in item["events"]]
    rumors = [rumor for item in ordered for rumor in item["rumors"]]
    source_rows: list[dict[str, str]] = []
    seen_sources: set[tuple[str, str]] = set()
    for item in ordered:
        key = item["artist"]["key"]
        for source in item["sources"]:
            dedupe_key = (key, source["url"])
            if dedupe_key in seen_sources:
                continue
            seen_sources.add(dedupe_key)
            source_rows.append({"artist_key": key, **source})

    completed = datetime.now(SHANGHAI_TZ)
    warnings = [
        "%s: %s" % (item["artist"]["name"], warning)
        for item in ordered for warning in item["warnings"]
    ]
    payload = {
        "_meta": {
            "researched_at": today,
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%S"),
            "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%S"),
            "by": "moonshot-formula-web-search-candidate",
            "model": model,
            "formula": FORMULA_URI,
            "artists_total": len(artists),
            "artists_succeeded": len(ordered),
            "events_found": len(events),
            "rumors_found": len(rumors),
            "sources_consulted": len(source_rows),
            "coverage": {
                item["artist"]["key"]: item["coverage"] for item in ordered
            },
            "queries": {
                item["artist"]["key"]: item["searches"] for item in ordered
            },
            "warnings": warnings,
            "note": (
                "全部 enabled 艺人已由代码执行四类 Kimi Formula 搜索；"
                "来源 URL 由程序从 Formula reference catalog 固定生成 source_id，"
                "模型只能引用 ID，URL 由程序回填并仅做被动语法/"
                "主机边界检查；可达性与逐字段内容支持仍待人工复核。"
            ),
        },
        "events": events,
        "rumors": rumors,
        "sources": source_rows,
    }
    if telemetry is not None:
        telemetry_snapshot = telemetry.snapshot(status="research_completed")
        payload["_meta"]["billing"] = telemetry_snapshot["summary"]
        payload["_meta"]["billing_by_artist"] = telemetry_snapshot["by_artist"]
        billing = telemetry_snapshot["summary"]
        estimated = billing.get("total_estimated_cost_cny")
        _log(
            "Kimi 用量：Formula %d 次 / Chat %d 次 / 输入 %d（缓存 %d）/ "
            "输出含推理 %d / 估算费用 %s" % (
                billing["formula_calls"], billing["chat_calls"],
                billing["prompt_tokens"], billing["cached_tokens"],
                billing["completion_tokens_including_reasoning"],
                "待控制台核对" if estimated is None else "¥%.4f" % estimated,
            )
        )
    return payload


def write_payload(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=output.parent,
            prefix=".%s." % output.name, delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def mark_candidate_only(payload: dict[str, Any]) -> dict[str, Any]:
    """Make every paid artifact explicitly ineligible for normal ingest."""
    meta = payload.setdefault("_meta", {})
    meta["promotion_status"] = "candidate_only_pending_manual_validation"
    meta["production_write"] = False
    meta["quality_gate"] = "blocked_all_evaluated_models_rejected"
    return payload


def validate_showstart_coverage(meta: dict[str, Any]) -> None:
    """完整刷新不允许把任何秀动降级当成成功发布。"""
    showstart_status = (meta.get("source_status") or {}).get("showstart") or {}
    config = monitor.load_config()
    showstart_expected = sum(
        1 for artist in monitor.enabled_artists(config)
        if not (artist.get("region") == "kpop" and not artist.get("showstart_artist_id"))
    )
    showstart_ok = int(showstart_status.get("ok") or 0)
    showstart_failed = int(showstart_status.get("fail") or 0)
    if showstart_failed or showstart_ok != showstart_expected:
        raise ResearchError(
            "秀动采集未完整（成功 %d / 应采 %d / 失败 %d），不发布本轮数据" % (
                showstart_ok, showstart_expected, showstart_failed,
            )
        )


def validate_production_store_inputs() -> None:
    """Tracked production stores must exist and parse before any collector writes."""
    required = (
        (ROOT / "data" / "events.json", {}),
        (ROOT / "data" / "rumors.json", {}),
        (ROOT / "data" / "meta.json", {"runs": [], "last_run": None}),
    )
    for path, default in required:
        if not path.is_file():
            raise ResearchError("缺少必需的生产数据文件：%s" % path)
        try:
            store._load(str(path), default)
        except store.StoreDataError as exc:
            raise ResearchError(str(exc)) from exc


def run_pipeline(*_args: Any, **_kwargs: Any) -> None:
    """Removed legacy all-or-nothing entry point kept only for shadow guards."""
    raise ResearchError(
        "legacy run_pipeline is disabled; use deterministic + validated inbox layers"
    )


def refresh_id() -> str:
    value = os.environ.get("FULL_REFRESH_ID", "").strip()
    if value and not re.fullmatch(r"[a-f0-9]{24}", value):
        raise ResearchError("FULL_REFRESH_ID 格式无效")
    if value:
        return value
    return "local-" + store.local_now().strftime("%Y%m%d-%H%M%S")


def run_deterministic_pipeline(
    showstart_sleep: float, showstart_workers: int,
) -> dict[str, Any]:
    """Collect and build the deterministic snapshot without touching inbox."""
    validate_production_store_inputs()
    command = [
        sys.executable, str(ROOT / "monitor.py"), "check",
        "--force", "--sleep", str(showstart_sleep),
        "--concurrent-workers", str(max(1, showstart_workers)),
        "--no-inbox",
        "--strict-sources",
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    meta = store.load_meta()
    validate_showstart_coverage(meta)
    _log("确定性采集与站点构建已完成：%s" % (meta.get("last_run") or store.now_iso()))
    return meta


def _safe_error(exc: Exception | None) -> str:
    if exc is None:
        return ""
    return re.sub(r"\s+", " ", _redact_sensitive(exc)).strip()[:300]


def _public_error_reason(exc: Exception | None) -> str:
    """Return an allowlisted metadata reason, never provider/refusal text."""
    if exc is None:
        return ""
    if isinstance(exc, QuotaError):
        return "quota_or_balance_unavailable"
    if isinstance(exc, EnrichmentBudgetError):
        return "budget_preflight_blocked"
    if isinstance(exc, EnrichmentContextError):
        return "context_preflight_blocked"
    message = str(exc).lower()
    if "disabled by configuration" in message:
        return "enrichment_disabled"
    if "api_key missing" in message or "缺少 moonshot_api_key" in message:
        return "missing_api_key"
    if "candidate_only_pending_manual_validation" in message:
        return "candidate_only_pending_manual_validation"
    if isinstance(exc, ResearchError):
        return "provider_or_candidate_validation_failed"
    return "internal_enrichment_failure"


def finalize_refresh_metadata(
    *, run_id: str, artists: list[dict[str, Any]], provider: str,
    enrichment_status: str, payload: dict[str, Any] | None = None,
    error: Exception | None = None,
    budget_reservation: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    """Publish truthful layered status; never reconcile or delete old research."""
    if enrichment_status not in {
        "disabled_stale", "skipped_stale",
        "budget_blocked_stale", "failed_stale", "candidate_ready_stale",
    }:
        raise ResearchError("invalid enrichment status")

    meta = store.load_meta()
    completed_at = store.now_iso()
    source_status = dict(meta.get("source_status") or {})
    previous_research_at = meta.get("last_research_at")
    stale = True
    candidate_meta = (payload or {}).get("_meta", {})
    model_name = candidate_meta.get("model") or (
        DEFAULT_MODEL if provider == "kimi" else ""
    )
    research_status: dict[str, Any] = {
        "ok": 0,
        "fail": len(artists) if enrichment_status == "failed_stale" else 0,
        "total": len(artists),
        "status": enrichment_status,
        "stale": stale,
        "provider": provider,
        "model": model_name,
        "last_success_at": previous_research_at,
    }
    if enrichment_status == "candidate_ready_stale":
        research_status["candidate_ok"] = int(
            candidate_meta.get("artists_succeeded") or 0
        )
    reason = _public_error_reason(error)
    if reason:
        research_status["reason"] = reason
    source_status["research"] = research_status
    showstart_status = dict(source_status.get("showstart") or {})
    source_warnings = bool(int(showstart_status.get("fail") or 0))
    if enrichment_status == "failed_stale":
        layered_status = "deterministic_completed_enrichment_failed"
    else:
        layered_status = "deterministic_completed_enrichment_stale"
    if source_warnings:
        layered_status += "_with_source_warnings"

    meta["source_status"] = source_status
    meta["full_refresh_at"] = completed_at
    meta["full_refresh_id"] = run_id
    meta["full_refresh_status"] = layered_status
    enrichment_record: dict[str, Any] = {
        "provider": provider,
        "status": enrichment_status,
        "stale": stale,
        "model": research_status["model"],
        "artists": len(artists),
        "events_found": 0,
        "rumors_found": 0,
        "sources_consulted": 0,
        "warnings": len(candidate_meta.get("warnings") or []),
        "last_success_at": research_status["last_success_at"],
        "reason": reason,
        "budget_reservation": budget_reservation,
        "write_policy": (
            "candidate_only_external_no_production_merge"
            if enrichment_status == "candidate_ready_stale"
            else "no_enrichment_write"
        ),
        "promotion_status": (
            "blocked_all_evaluated_models_rejected"
            if enrichment_status == "candidate_ready_stale"
            else (
                "not_requested" if provider == "none"
                else "candidate_not_produced"
            )
        ),
    }
    if enrichment_status == "candidate_ready_stale":
        enrichment_record.update({
            "candidate_events_found": int(candidate_meta.get("events_found") or 0),
            "candidate_rumors_found": int(candidate_meta.get("rumors_found") or 0),
            "candidate_sources_consulted": int(
                candidate_meta.get("sources_consulted") or 0
            ),
        })

    meta["full_refresh"] = {
        "architecture": "deterministic_with_optional_enrichment",
        "id": run_id,
        "at": completed_at,
        "status": layered_status,
        "deterministic": {
            "status": "completed_with_source_warnings" if source_warnings else "completed",
            "showstart": showstart_status,
        },
        "enrichment": enrichment_record,
    }
    if stale:
        notes = list(meta.get("notes") or [])
        notes.append(
            "确定性刷新已完成；可选模型增强为 %s，旧 research 数据保留并标记 stale%s"
            % (enrichment_status, ("：" + reason) if reason else "")
        )
        meta["notes"] = notes
    store.save_meta(meta)
    monitor.build_site()
    _log("分层刷新状态：%s" % layered_status)
    return meta


def default_research_only_output_path() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="concert-research-only-"))
    return directory / "candidate.json"


def default_candidate_output_path() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="concert-enrichment-candidate-"))
    return directory / "candidate.json"


def _resolve_external_new_json(path: Path, label: str) -> Path:
    """Candidate artifacts are external, non-symlinked and never overwritten."""
    resolved = path.expanduser().resolve()
    repository = ROOT.resolve()
    if (
        resolved == repository
        or repository in resolved.parents
        or resolved == Path(resolved.anchor)
        or resolved.suffix.lower() != ".json"
    ):
        raise ResearchError("%s 输出必须是项目目录外的 JSON 文件" % label)
    if resolved.exists():
        raise ResearchError("%s 输出已存在，拒绝覆盖" % label)
    return resolved


class ReservedJsonOutput:
    """Hold an exclusive external output inode across all paid requests."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = _resolve_external_new_json(path, label)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir so a pre-existing symlinked parent cannot escape.
        self.path = _resolve_external_new_json(self.path, label)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd = os.open(str(self.path), flags, 0o600)
        except FileExistsError as exc:
            raise ResearchError("%s 输出已存在，拒绝覆盖" % label) from exc
        except OSError as exc:
            raise ResearchError("%s 输出不可写" % label) from exc
        stat = os.fstat(self._fd)
        self._identity = (stat.st_dev, stat.st_ino)
        self._closed = False

    def commit(self, payload: dict[str, Any]) -> None:
        if self._closed:
            raise ResearchError("candidate output reservation is closed")
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(self._fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(self._fd)
        except Exception:
            self.discard()
            raise
        os.close(self._fd)
        self._closed = True

    def discard(self) -> None:
        if not self._closed:
            try:
                os.close(self._fd)
            finally:
                self._closed = True
        try:
            stat = self.path.lstat()
        except FileNotFoundError:
            return
        if (stat.st_dev, stat.st_ino) == self._identity and not self.path.is_symlink():
            self.path.unlink()


def resolve_research_only_output(path: Path) -> Path:
    return _resolve_external_new_json(path, "research-only")


def resolve_candidate_output(path: Path) -> Path:
    return _resolve_external_new_json(path, "candidate")


def default_telemetry_path() -> Path:
    refresh_id = os.environ.get("FULL_REFRESH_ID", "").strip()
    suffix = refresh_id if re.fullmatch(r"[a-f0-9]{24}", refresh_id) else datetime.now(
        SHANGHAI_TZ,
    ).strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / ("concert-refresh-telemetry-%s.json" % suffix)


def resolve_telemetry_output(path: Path) -> Path:
    """Keep billing artifacts out of every production-owned repository path."""
    resolved = path.expanduser().resolve()
    repository = ROOT.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ResearchError("telemetry 必须写入项目目录之外")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _git_head() -> str:
    github_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    if re.fullmatch(r"[a-f0-9]{40}", github_sha):
        return github_sha
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""
    return value if re.fullmatch(r"[a-f0-9]{40}", value) else ""


def validate_paid_enrichment_endpoint() -> str:
    """Pinned CNY pricing is valid only for Moonshot's China native endpoint."""
    raw = os.environ.get("MOONSHOT_API_BASE", "https://api.moonshot.cn/v1")
    try:
        parsed = urllib.parse.urlsplit(raw.rstrip("/"))
        port = parsed.port
    except ValueError as exc:
        raise EnrichmentBudgetError("invalid MOONSHOT_API_BASE") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "api.moonshot.cn"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise EnrichmentBudgetError(
            "paid Kimi enrichment is pinned to https://api.moonshot.cn/v1"
        )
    return "api.moonshot.cn"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="确定性采集/构建 + 可选、预算受限的模型增强",
    )
    parser.add_argument(
        "--model", default=os.environ.get("KIMI_RESEARCH_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--output", type=Path,
        help="仅供 --research-only 的仓库外 candidate JSON",
    )
    candidate_default = os.environ.get(
        "REFRESH_ENRICH_CANDIDATE_OUTPUT", "",
    ).strip()
    parser.add_argument(
        "--candidate-output", type=Path,
        default=Path(candidate_default) if candidate_default else None,
        help="分层刷新的仓库外 candidate artifact；绝不自动 merge",
    )
    parser.add_argument(
        "--enrich-provider", choices=ENRICH_PROVIDERS,
        default=os.environ.get("LLM_ENRICH_PROVIDER", DEFAULT_ENRICH_PROVIDER),
        help="可选模型增强；默认 none，只有显式 kimi 才会调用付费 API",
    )
    parser.add_argument(
        "--enrich-max-cost-cny",
        default=os.environ.get(
            "LLM_ENRICH_MAX_COST_CNY", str(DEFAULT_ENRICH_GLOBAL_BUDGET_CNY),
        ),
    )
    parser.add_argument(
        "--enrich-max-cost-per-artist-cny",
        default=os.environ.get(
            "LLM_ENRICH_MAX_COST_PER_ARTIST_CNY",
            str(DEFAULT_ENRICH_ARTIST_BUDGET_CNY),
        ),
    )
    parser.add_argument(
        "--enrich-max-context-bytes",
        default=os.environ.get(
            "LLM_ENRICH_MAX_CONTEXT_BYTES", str(DEFAULT_ENRICH_CONTEXT_BYTES),
        ),
    )
    parser.add_argument(
        "--enrich-attempts",
        default=os.environ.get(
            "LLM_ENRICH_ATTEMPTS", str(DEFAULT_ENRICH_ATTEMPTS),
        ),
        help="每个付费阶段的应用级尝试上限；生产默认 1",
    )
    telemetry_default = os.environ.get("REFRESH_TELEMETRY_OUTPUT", "").strip()
    parser.add_argument(
        "--telemetry-output", type=Path,
        default=Path(telemetry_default) if telemetry_default else None,
        help="逐调用用量与估算费用 JSON；默认写入系统临时目录",
    )
    parser.add_argument(
        "--research-only", action="store_true",
        help="只产出调研 JSON，不执行 monitor check/ingest/build",
    )
    parser.add_argument(
        "--artist-key",
        help="仅供 --research-only 冒烟测试：只调研指定艺人",
    )
    parser.add_argument("--showstart-sleep", type=float, default=0.15)
    parser.add_argument(
        "--showstart-workers", type=int, default=3,
        help="秀动艺人并发数（默认 3）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.enrich_provider not in ENRICH_PROVIDERS:
        print("错误：LLM_ENRICH_PROVIDER 只允许 none 或 kimi。", file=sys.stderr)
        return 2
    if args.workers < 1 or args.showstart_workers < 1 or args.showstart_sleep < 0:
        print("错误：workers 必须 >=1，showstart sleep 必须 >=0。", file=sys.stderr)
        return 2
    optional_config_error: EnrichmentBudgetError | None = None
    if args.enrich_provider == "kimi":
        try:
            args.enrich_max_cost_cny = float(args.enrich_max_cost_cny)
            args.enrich_max_cost_per_artist_cny = float(
                args.enrich_max_cost_per_artist_cny
            )
            args.enrich_max_context_bytes = int(args.enrich_max_context_bytes)
            args.enrich_attempts = int(args.enrich_attempts)
        except (TypeError, ValueError):
            optional_config_error = EnrichmentBudgetError(
                "invalid optional enrichment budget/context configuration"
            )
            # Keep telemetry serializable; production Kimi will be blocked later.
            args.enrich_max_cost_cny = DEFAULT_ENRICH_GLOBAL_BUDGET_CNY
            args.enrich_max_cost_per_artist_cny = DEFAULT_ENRICH_ARTIST_BUDGET_CNY
            args.enrich_max_context_bytes = DEFAULT_ENRICH_CONTEXT_BYTES
            args.enrich_attempts = DEFAULT_ENRICH_ATTEMPTS
    else:
        # Malformed optional repo vars must never block the free deterministic layer.
        args.enrich_max_cost_cny = DEFAULT_ENRICH_GLOBAL_BUDGET_CNY
        args.enrich_max_cost_per_artist_cny = DEFAULT_ENRICH_ARTIST_BUDGET_CNY
        args.enrich_max_context_bytes = DEFAULT_ENRICH_CONTEXT_BYTES
        args.enrich_attempts = DEFAULT_ENRICH_ATTEMPTS
    try:
        run_id = refresh_id()
    except ResearchError as exc:
        print("错误：%s。" % _safe_error(exc), file=sys.stderr)
        return 2
    cfg = monitor.load_config()
    artists = monitor.enabled_artists(cfg)
    if args.artist_key:
        if not args.research_only:
            print("错误：--artist-key 只能与 --research-only 一起使用。", file=sys.stderr)
            return 2
        artists = [artist for artist in artists if artist["key"] == args.artist_key]
    if not artists:
        print("错误：没有匹配的 enabled 艺人。", file=sys.stderr)
        return 2
    if not args.research_only and args.output is not None:
        print(
            "错误：--output 只能与 --research-only 一起使用；"
            "生产刷新不允许写 inbox。",
            file=sys.stderr,
        )
        return 2
    try:
        research_output = (
            resolve_research_only_output(
                args.output or default_research_only_output_path()
            )
            if args.research_only else None
        )
    except ResearchError as exc:
        print("错误：%s。" % _safe_error(exc), file=sys.stderr)
        return 2
    try:
        telemetry_output = resolve_telemetry_output(
            args.telemetry_output or default_telemetry_path()
        )
    except ResearchError as exc:
        print("错误：%s。" % _safe_error(exc), file=sys.stderr)
        return 2
    telemetry_refresh_id = os.environ.get("FULL_REFRESH_ID", "").strip()
    if not re.fullmatch(r"[a-f0-9]{24}", telemetry_refresh_id):
        telemetry_refresh_id = ""
    moonshot_api_host = ""
    if args.enrich_provider == "kimi":
        try:
            moonshot_api_host = (
                urllib.parse.urlsplit(
                    os.environ.get(
                        "MOONSHOT_API_BASE", "https://api.moonshot.cn/v1",
                    )
                ).hostname or ""
            ).lower().rstrip(".")
        except ValueError:
            moonshot_api_host = ""
    telemetry = RefreshTelemetry(args.model, context={
        "refresh_id": telemetry_refresh_id,
        "git_head": _git_head(),
        "research_only": bool(args.research_only),
        "architecture": "deterministic_with_optional_enrichment",
        "enrich_provider": args.enrich_provider,
        "enrich_enabled": args.enrich_provider != "none",
        "artist_keys": [artist["key"] for artist in artists],
        "workers": args.workers,
        "formula": FORMULA_URI,
        "api_host": moonshot_api_host,
        "pricing_region": (
            "moonshot_cn" if moonshot_api_host == "api.moonshot.cn" else "unknown"
        ),
        "reasoning_effort": "high",
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "max_context_bytes": args.enrich_max_context_bytes,
        "context_preflight_method": "full_request_utf8_bytes",
        "global_budget_cny": args.enrich_max_cost_cny,
        "per_artist_budget_cny": args.enrich_max_cost_per_artist_cny,
        "attempts_per_paid_stage": args.enrich_attempts,
        "source_contract": "program_catalog_source_id_url_backfill",
        "time_contract": "doors_show_end_curfew_distinct",
        "files_sha256": {
            "config/artists.json": _file_sha256(ROOT / "config" / "artists.json"),
            "scripts/full_refresh.py": _file_sha256(Path(__file__).resolve()),
        },
    })
    try:
        telemetry.write(telemetry_output, status="prepared")
    except OSError as exc:
        print(
            "错误：无法创建刷新计量文件：%s" % _safe_error(exc),
            file=sys.stderr,
        )
        return 2
    final_status = "failed_before_deterministic_publish"
    error_type: str | None = None
    exit_code = 0
    if args.research_only:
        research_reservation: ReservedJsonOutput | None = None
        try:
            if args.enrich_provider != "kimi":
                raise ResearchError(
                    "--research-only 需要显式 --enrich-provider kimi 才能调用付费 API"
                )
            if not os.environ.get("MOONSHOT_API_KEY", "").strip():
                raise ResearchError("缺少 MOONSHOT_API_KEY")
            if optional_config_error is not None:
                raise optional_config_error
            validate_production_store_inputs()
            if research_output is None:
                raise ResearchError("research-only output was not resolved")
            research_reservation = ReservedJsonOutput(
                research_output, "research-only",
            )
            validate_paid_enrichment_endpoint()
            preflight_enrichment_budget(
                artist_count=len(artists), model=args.model,
                global_limit_cny=args.enrich_max_cost_cny,
                per_artist_limit_cny=args.enrich_max_cost_per_artist_cny,
                max_context_bytes=args.enrich_max_context_bytes,
                attempts=args.enrich_attempts,
            )
            payload = research_all(
                artists, args.model, args.workers,
                requester=call_chat_api_once,
                search_requester=call_formula_api_once,
                telemetry=telemetry,
                chat_retries=args.enrich_attempts,
                search_retries=args.enrich_attempts,
                max_context_bytes=args.enrich_max_context_bytes,
                fail_fast=True,
            )
            mark_candidate_only(payload)
            research_reservation.commit(payload)
            research_reservation = None
            _log("Kimi 调研 JSON 已写入：%s" % research_output)
            final_status = "research_only_completed"
        except Exception as exc:
            error_type = type(exc).__name__
            print("模型调研失败：%s" % _safe_error(exc), file=sys.stderr)
            exit_code = 1
        finally:
            if research_reservation is not None:
                research_reservation.discard()
            try:
                telemetry.write(
                    telemetry_output, status=final_status, error_type=error_type,
                )
                _log("刷新计量 JSON 已写入：%s" % telemetry_output)
            except OSError as exc:
                print(
                    "错误：刷新计量文件写入失败：%s"
                    % _safe_error(exc),
                    file=sys.stderr,
                )
                exit_code = 1
        return exit_code

    try:
        run_deterministic_pipeline(args.showstart_sleep, args.showstart_workers)
    except (ResearchError, subprocess.CalledProcessError, OSError) as exc:
        error_type = type(exc).__name__
        print("确定性刷新失败：%s" % _safe_error(exc), file=sys.stderr)
        exit_code = 1
    else:
        enrichment_status = "disabled_stale"
        enrichment_error: Exception | None = ResearchError(
            "LLM enrichment disabled by configuration"
        )
        budget_reservation: dict[str, float | int] | None = None
        payload: dict[str, Any] | None = None
        if args.enrich_provider == "kimi":
            if not os.environ.get("MOONSHOT_API_KEY", "").strip():
                enrichment_status = "skipped_stale"
                enrichment_error = ResearchError("MOONSHOT_API_KEY missing")
            else:
                candidate_reservation: ReservedJsonOutput | None = None
                try:
                    if optional_config_error is not None:
                        raise optional_config_error
                    candidate_output = resolve_candidate_output(
                        args.candidate_output or default_candidate_output_path()
                    )
                    candidate_reservation = ReservedJsonOutput(
                        candidate_output, "candidate",
                    )
                    validate_paid_enrichment_endpoint()
                    budget_reservation = preflight_enrichment_budget(
                        artist_count=len(artists), model=args.model,
                        global_limit_cny=args.enrich_max_cost_cny,
                        per_artist_limit_cny=args.enrich_max_cost_per_artist_cny,
                        max_context_bytes=args.enrich_max_context_bytes,
                        attempts=args.enrich_attempts,
                    )
                    payload = research_all(
                        artists, args.model, args.workers,
                        requester=call_chat_api_once,
                        search_requester=call_formula_api_once,
                        telemetry=telemetry,
                        chat_retries=args.enrich_attempts,
                        search_retries=args.enrich_attempts,
                        max_context_bytes=args.enrich_max_context_bytes,
                        fail_fast=True,
                    )
                    mark_candidate_only(payload)
                    candidate_reservation.commit(payload)
                    candidate_reservation = None
                    telemetry.context["candidate_artifact_created"] = True
                    telemetry.context["candidate_artifact_sha256"] = _file_sha256(
                        candidate_output
                    )
                    telemetry.context["promotion_status"] = (
                        "blocked_all_evaluated_models_rejected"
                    )
                    _log(
                        "Kimi candidate 已写入仓库外 artifact，"
                        "未进入 inbox/生产数据：%s" % candidate_output
                    )
                    enrichment_status = "candidate_ready_stale"
                    enrichment_error = ResearchError(
                        "candidate_only_pending_manual_validation"
                    )
                except EnrichmentBudgetError as exc:
                    enrichment_status = "budget_blocked_stale"
                    enrichment_error = exc
                except Exception as exc:
                    enrichment_status = "failed_stale"
                    enrichment_error = exc
                finally:
                    if candidate_reservation is not None:
                        candidate_reservation.discard()
        try:
            finalized = finalize_refresh_metadata(
                run_id=run_id, artists=artists, provider=args.enrich_provider,
                enrichment_status=enrichment_status, payload=payload,
                error=enrichment_error, budget_reservation=budget_reservation,
            )
            final_status = finalized["full_refresh_status"]
            if enrichment_status in {
                "failed_stale", "budget_blocked_stale", "skipped_stale",
            } and enrichment_error is not None:
                error_type = type(enrichment_error).__name__
        except Exception as exc:
            error_type = type(exc).__name__
            print(
                "确定性快照状态写入失败：%s" % _safe_error(exc),
                file=sys.stderr,
            )
            exit_code = 1

    try:
        telemetry.write(
            telemetry_output, status=final_status, error_type=error_type,
        )
        _log("刷新计量 JSON 已写入：%s" % telemetry_output)
    except OSError as exc:
        print(
            "错误：刷新计量文件写入失败：%s"
            % _safe_error(exc),
            file=sys.stderr,
        )
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
