#!/usr/bin/env python3
"""完整手动刷新：Kimi 全员联网调研 + 秀动采集 + 合并 + 构建。

联网调研使用 Kimi K3 与官方 ``moonshot/web-search:latest`` Formula。
每位 enabled 艺人的票务、官方、中国区域/完整巡演和近期舆情四类搜索
都由代码直接执行；只有全部艺人和秀动采集均成功后才发布新快照。

用法：
    MOONSHOT_API_KEY=... python3 scripts/full_refresh.py
    MOONSHOT_API_KEY=... python3 scripts/full_refresh.py \
        --research-only --output /tmp/research.json

可选环境变量：
    KIMI_RESEARCH_MODEL    默认 kimi-k3
    MOONSHOT_API_BASE      默认 https://api.moonshot.cn/v1
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
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
import monitor  # noqa: E402


DEFAULT_MODEL = "kimi-k3"
DEFAULT_WORKERS = 1
DEFAULT_TIMEOUT = 300
MAX_RETRIES = 3
MAX_HTTP_RETRIES = 5
FORMULA_URI = "moonshot/web-search:latest"
FORMULA_TOOL_NAME = "web_search"
SHANGHAI_TZ = store.APP_TIMEZONE
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?$")
PRINT_LOCK = threading.Lock()
URL_CACHE_LOCK = threading.Lock()
URL_REACHABILITY_CACHE: dict[str, bool] = {}
API_RATE_LOCK = threading.Lock()
LAST_API_REQUEST_AT = 0.0
SEARCH_CATEGORIES = ("ticketing", "official", "china_region", "rumors")


class ResearchError(RuntimeError):
    """调研 API、联网来源或结果校验失败。"""


class QuotaError(ResearchError):
    """Moonshot 账户余额/额度不足，重试不会自愈。"""


EVENT_PROPERTIES = {
    "url": {"type": "string"},
    "tour_name": {"type": "string"},
    "title": {"type": "string"},
    "city": {"type": "string"},
    "country": {"type": "string"},
    "venue": {"type": "string"},
    "show_date": {"type": "string"},
    "show_time": {"type": "string"},
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
    "url": {"type": "string"},
    "credibility": {"type": "string", "enum": ["high", "medium", "low"]},
    "posted_at": {"type": "string"},
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
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": SOURCE_PROPERTIES,
                "required": list(SOURCE_PROPERTIES),
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
    "required": ["events", "rumors", "sources", "coverage"],
}


def _log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _existing_context(artist: dict[str, Any]) -> dict[str, Any]:
    """把现有数据作为待复核线索，不当作事实直接复制。"""
    key = artist["key"]
    events = [
        value for value in _load_json(ROOT / "data" / "events.json", {}).values()
        if value.get("artist_key") == key and store.derive_status(value) != "ended"
    ]
    rumors = [
        value for value in _load_json(ROOT / "data" / "rumors.json", {}).values()
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
- sources 只列四组工具结果中真实出现的公开页面；category 必须标明来自哪组结果。
- 每条 event/rumor 的 URL 必须原样出现在 sources；没有依据 URL 就不输出。
- 官方或正式票务可查才标 confirmed。论坛/搬运/曝光放 rumors。
- 不猜日期、时间、价格或场馆；不确定的字段留空。posted_at 必须为 YYYY-MM-DD。
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


def _moonshot_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
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
    for attempt in range(MAX_HTTP_RETRIES):
        try:
            _pace_moonshot_request()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:1200]
            last_error = ResearchError("Kimi HTTP %s: %s" % (exc.code, body))
            if exc.code == 429 and _quota_exhausted(body):
                raise QuotaError(str(last_error)) from exc
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise last_error from exc
            if attempt + 1 < MAX_HTTP_RETRIES:
                delay = _retry_delay(attempt, exc.headers)
                _log("  ! Kimi HTTP %s，%.0f 秒后重试" % (exc.code, delay))
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = ResearchError("Kimi 请求失败: %s" % exc)
            if attempt + 1 < MAX_HTTP_RETRIES:
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


FormulaRequester = Callable[[dict[str, Any]], dict[str, Any]]
ChatRequester = Callable[[dict[str, Any]], dict[str, Any]]
URLChecker = Callable[[str], bool]


def _formula_output(fiber: dict[str, Any]) -> str:
    if fiber.get("status") != "succeeded":
        raise ResearchError("Kimi Formula 执行失败: %s" % (
            fiber.get("error") or fiber.get("status") or "unknown",
        ))
    context = fiber.get("context") or {}
    output = context.get("output") or context.get("encrypted_output") or ""
    if not isinstance(output, str) or not output.strip():
        raise ResearchError("Kimi Formula 搜索没有返回上下文")
    return output


def execute_searches(
    artist: dict[str, Any],
    today: str,
    requester: FormulaRequester = call_formula_api,
) -> list[dict[str, str]]:
    executions: list[dict[str, str]] = []
    for index, item in enumerate(build_search_queries(artist, today)):
        arguments = json.dumps(
            {"query": item["query"]}, ensure_ascii=False, separators=(",", ":"),
        )
        body = {"name": FORMULA_TOOL_NAME, "arguments": arguments}
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                fiber = requester(body)
                output = _formula_output(fiber)
                executions.append({
                    **item,
                    "tool_call_id": "%s:%d" % (FORMULA_TOOL_NAME, index),
                    "fiber_id": str(fiber.get("id") or ""),
                    "output": output,
                })
                break
            except QuotaError:
                raise
            except (ResearchError, OSError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    delay = 2 ** (attempt - 1)
                    _log("  ! %-14s %s 搜索失败，%d 秒后重试：%s" % (
                        artist["name"], item["category"], delay, exc,
                    ))
                    time.sleep(delay)
        else:
            raise ResearchError("%s 搜索连续 %d 次失败: %s" % (
                item["category"], MAX_RETRIES, last_error,
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
    searches: list[dict[str, str]],
) -> dict[str, Any]:
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
                "schema": RESULT_SCHEMA,
            },
        },
        "reasoning_effort": "high",
        "max_completion_tokens": 16000,
    }


def _output_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ResearchError("Kimi 响应没有 choices")
    choice = choices[0] or {}
    message = choice.get("message") or {}
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


def _public_http_url(url: str, resolve: bool = True) -> bool:
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
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return "." in host
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        if not _public_http_url(newurl):
            raise urllib.error.URLError("redirect target is not a public HTTP URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def source_url_reachable(url: str, timeout: int = 7) -> bool:
    identity = urllib.parse.urlunsplit((*urllib.parse.urlsplit(url)[:4], ""))
    with URL_CACHE_LOCK:
        if identity in URL_REACHABILITY_CACHE:
            return URL_REACHABILITY_CACHE[identity]
    if not _public_http_url(url):
        result = False
    else:
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        gated = False
        result = False
        for method in ("HEAD", "GET"):
            headers = {
                "User-Agent": "Mozilla/5.0 concert-monitor-source-check/1.0",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
            }
            if method == "GET":
                headers["Range"] = "bytes=0-4095"
            request = urllib.request.Request(url, headers=headers, method=method)
            try:
                with opener.open(request, timeout=timeout) as response:
                    final_url = response.geturl()
                    result = 200 <= response.status < 400 and _public_http_url(final_url)
                if result:
                    break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    gated = True
                    break
                if method == "HEAD" and exc.code not in (405, 429, 500, 501, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                pass
        result = result or gated
    with URL_CACHE_LOCK:
        URL_REACHABILITY_CACHE[identity] = result
    return result


def _validate_result(
    artist: dict[str, Any],
    response: dict[str, Any],
    searches: list[dict[str, str]],
    url_checker: URLChecker = source_url_reachable,
) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
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
    sources: list[dict[str, str]] = []
    referenced_urls = {
        _url_identity(item["url"])
        for item in [*result["events"], *result["rumors"]]
        if _public_http_url(item["url"], resolve=False)
    }
    source_candidates: list[tuple[int, dict[str, str], tuple[str, str, int | None, str, str]]] = []
    seen_source_ids: set[tuple[str, str, int | None, str, str]] = set()
    for index, raw in enumerate(result["sources"]):
        identity = _url_identity(raw["url"])
        if not identity[0] or not _public_http_url(raw["url"], resolve=False):
            warnings.append("source[%d] 不是安全的公开 HTTP(S) URL，已丢弃" % index)
            continue
        if identity in seen_source_ids:
            continue
        if identity not in referenced_urls:
            continue
        seen_source_ids.add(identity)
        if len(source_candidates) < 40:
            source_candidates.append((index, raw, identity))
    if len(seen_source_ids) > 40:
        warnings.append("本轮实际引用来源超过 40 条，仅校验并保留前 40 条")

    reachability: dict[tuple[str, str, int | None, str, str], bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(source_candidates) or 1)) as executor:
        future_map = {
            executor.submit(url_checker, raw["url"]): (index, raw, identity)
            for index, raw, identity in source_candidates
        }
        for future in concurrent.futures.as_completed(future_map):
            index, raw, identity = future_map[future]
            try:
                reachability[identity] = bool(future.result())
            except Exception:
                reachability[identity] = False
            if not reachability[identity]:
                warnings.append("source[%d] URL 无法访问，已丢弃" % index)
    sources = [
        raw for _, raw, identity in source_candidates if reachability.get(identity)
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


def research_artist(
    artist: dict[str, Any],
    model: str,
    today: str,
    tools: list[dict[str, Any]],
    requester: ChatRequester = call_chat_api,
    search_requester: FormulaRequester = call_formula_api,
    url_checker: URLChecker = source_url_reachable,
    retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    searches = execute_searches(artist, today, search_requester)
    payload = build_request(artist, model, today, tools, searches)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requester(payload)
            result, sources, warnings = _validate_result(
                artist, response, searches, url_checker,
            )
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
            raise
        except (ResearchError, OSError) as exc:
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
    url_checker: URLChecker = source_url_reachable,
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
                )
                record_result(artist, value)
            except QuotaError:
                raise
            except Exception as exc:
                failures.append("%s: %s" % (artist["name"], exc))
                _log("  ! %-14s 失败：%s" % (artist["name"], exc))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    research_artist, artist, model, today, formula_tools,
                    requester, search_requester, url_checker,
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
    return {
        "_meta": {
            "researched_at": today,
            "started_at": started.strftime("%Y-%m-%dT%H:%M:%S"),
            "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%S"),
            "by": "kimi-k3-formula-web-search",
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
                "来源 URL 由 Kimi 从受保护搜索上下文整理，并通过公开地址可达性校验。"
            ),
        },
        "events": events,
        "rumors": rumors,
        "sources": source_rows,
    }


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


def run_pipeline(
    payload: dict[str, Any], output: Path,
    showstart_sleep: float, showstart_workers: int,
) -> None:
    """调用现有 monitor check：采集秀动、ingest inbox 并 build。"""
    write_payload(payload, output)
    _log("Kimi 调研数据已校验：%s" % output)
    command = [
        sys.executable, str(ROOT / "monitor.py"), "check",
        "--force", "--sleep", str(showstart_sleep),
        "--concurrent-workers", str(max(1, showstart_workers)),
        "--strict-sources",
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    if output.exists():
        raise ResearchError("monitor check 未并入刚生成的调研文件")

    meta = store.load_meta()
    validate_showstart_coverage(meta)
    completed_at = store.now_iso()
    refresh_id = os.environ.get("FULL_REFRESH_ID", "").strip()
    if refresh_id and not re.fullmatch(r"[a-f0-9]{24}", refresh_id):
        raise ResearchError("FULL_REFRESH_ID 格式无效")
    if not refresh_id:
        refresh_id = "local-" + completed_at.replace("-", "").replace(":", "")
    reconciliation = store.reconcile_full_refresh(
        meta.get("last_run_id") or "",
        refresh_id,
        [artist["key"] for artist in monitor.enabled_artists(monitor.load_config())],
        completed_at,
    )
    source_status = dict(meta.get("source_status") or {})
    source_status["research"] = {
        "ok": payload["_meta"]["artists_succeeded"],
        "fail": 0,
        "total": payload["_meta"]["artists_total"],
    }
    research_warnings = payload["_meta"].get("warnings") or []
    warning_count = len(research_warnings)
    unverified_count = (
        reconciliation["events_unverified"] + reconciliation["rumors_unverified"]
    )
    meta["source_status"] = source_status
    meta["last_research_at"] = completed_at
    meta["full_refresh_at"] = completed_at
    meta["full_refresh_id"] = refresh_id
    meta["full_refresh_status"] = (
        "completed_with_warnings" if warning_count or unverified_count else "completed"
    )
    meta["full_refresh"] = {
        "id": refresh_id,
        "at": completed_at,
        "model": payload["_meta"]["model"],
        "artists": payload["_meta"]["artists_total"],
        "events_found": payload["_meta"]["events_found"],
        "rumors_found": payload["_meta"]["rumors_found"],
        "sources_consulted": payload["_meta"]["sources_consulted"],
        "warnings": warning_count,
        "unverified_records": unverified_count,
        "reconciliation": reconciliation,
    }
    if research_warnings:
        notes = list(meta.get("notes") or [])
        notes.append("完整调研丢弃了 %d 条无法验证或格式无效的候选信息" % (
            warning_count,
        ))
        meta["notes"] = notes
    if unverified_count:
        notes = list(meta.get("notes") or [])
        notes.append("本轮未再次搜到 %d 条旧记录，已标记“本轮未复核”但未盲删" % (
            unverified_count,
        ))
        meta["notes"] = notes
    store.save_meta(meta)
    monitor.build_site()
    _log("完整刷新已完成：%s" % completed_at)


def default_output_path() -> Path:
    stamp = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d-%H%M%S")
    return ROOT / "research" / "inbox" / (stamp + "-full-refresh.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kimi 全员、全信息源的完整手动刷新")
    parser.add_argument(
        "--model", default=os.environ.get("KIMI_RESEARCH_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--output", type=Path)
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
    if not os.environ.get("MOONSHOT_API_KEY", "").strip():
        print("错误：请先配置 MOONSHOT_API_KEY。", file=sys.stderr)
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
    output = (args.output or default_output_path()).resolve()
    try:
        payload = research_all(artists, args.model, args.workers)
        if args.research_only:
            write_payload(payload, output)
            _log("Kimi 调研 JSON 已写入：%s" % output)
        else:
            if ROOT not in output.parents:
                raise ResearchError("完整流程的 --output 必须位于项目目录内")
            run_pipeline(
                payload, output, args.showstart_sleep, args.showstart_workers,
            )
    except (ResearchError, subprocess.CalledProcessError) as exc:
        print("完整刷新失败：%s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
