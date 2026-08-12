"""在临时数据副本上执行一次秀动刷新，并把新站点快照返回给浏览器。"""

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import monitor  # noqa: E402
from lib import http, store  # noqa: E402


CACHE_TTL_SECONDS = 45
# 控制按钮等待时间：预留冷启动、合并与序列化时间，不把预算全交给上游。
REFRESH_BUDGET_SECONDS = 42
REQUEST_TIMEOUT_SECONDS = 5
CONCURRENT_ARTISTS = 3
_REFRESH_LOCK = threading.Lock()
_CACHE_PAYLOAD = None
_CACHE_AT = 0.0


def _cache_get(now=None):
    now = time.monotonic() if now is None else now
    if (_CACHE_PAYLOAD is not None and _CACHE_AT is not None
            and now - _CACHE_AT < CACHE_TTL_SECONDS):
        return copy.deepcopy(_CACHE_PAYLOAD)
    return None


def _copy_seed_data(temp_root):
    """复制仓库种子数据；HTTP 缓存不参与刷新，也不复制。"""
    shutil.copytree(REPO_ROOT / "config", temp_root / "config")
    source_data = REPO_ROOT / "data"
    if source_data.is_dir():
        shutil.copytree(
            source_data, temp_root / "data",
            ignore=shutil.ignore_patterns(".cache"),
        )
    else:
        (temp_root / "data").mkdir()
    (temp_root / "site").mkdir()


def _redirect_paths(temp_root):
    """把会写盘的模块全局路径指向临时目录，并返回原值。"""
    data_dir = temp_root / "data"
    paths = {
        (monitor, "ROOT"): str(temp_root),
        (monitor, "CONFIG_PATH"): str(temp_root / "config" / "artists.json"),
        (monitor, "SITE_DIR"): str(temp_root / "site"),
        (monitor, "INBOX_DIR"): str(temp_root / "research" / "inbox"),
        (monitor, "ARCHIVE_DIR"): str(temp_root / "research" / "archive"),
        (store, "ROOT"): str(temp_root),
        (store, "DATA_DIR"): str(data_dir),
        (store, "EVENTS_PATH"): str(data_dir / "events.json"),
        (store, "RUMORS_PATH"): str(data_dir / "rumors.json"),
        (store, "META_PATH"): str(data_dir / "meta.json"),
        (store, "CHANGELOG_PATH"): str(data_dir / "changes.log"),
        (http, "ROOT"): str(temp_root),
        (http, "CACHE_DIR"): str(data_dir / ".cache"),
    }
    original = {key: getattr(*key) for key in paths}
    for (module, name), value in paths.items():
        setattr(module, name, value)
    return original


def _restore_paths(original):
    for (module, name), value in original.items():
        setattr(module, name, value)


def _run_isolated_refresh():
    with tempfile.TemporaryDirectory(prefix="concert-refresh-") as tmp:
        temp_root = Path(tmp)
        _copy_seed_data(temp_root)
        original = _redirect_paths(temp_root)
        original_http_get = http.get
        deadline = time.monotonic() + REFRESH_BUDGET_SECONDS

        def budgeted_http_get(*args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                return None, "本轮在线刷新时间预算已用尽"
            requested_timeout = kwargs.get("timeout", REQUEST_TIMEOUT_SECONDS)
            kwargs["timeout"] = max(
                0.1,
                min(float(requested_timeout), REQUEST_TIMEOUT_SECONDS, remaining),
            )
            return original_http_get(*args, **kwargs)

        # showstart 引用的是同一个 http 模块；API 锁保证替换期间没有第二轮刷新。
        http.get = budgeted_http_get
        try:
            monitor.cmd_check(argparse.Namespace(
                force=True,
                sleep=0.05,
                no_inbox=True,
                concurrent_workers=CONCURRENT_ARTISTS,
            ))
            output_path = temp_root / "site" / "data.json"
            with output_path.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data.get("generated_at"):
                raise RuntimeError("刷新结果格式无效")
            return data
        finally:
            http.get = original_http_get
            _restore_paths(original)


def refresh_snapshot():
    """返回 ``(站点数据, 是否命中内存缓存)``。"""
    global _CACHE_AT, _CACHE_PAYLOAD

    cached = _cache_get()
    if cached is not None:
        return cached, True

    # 模块路径重定向依赖进程全局变量，因此刷新全程必须串行。
    with _REFRESH_LOCK:
        cached = _cache_get()
        if cached is not None:
            return cached, True
        data = _run_isolated_refresh()
        # 降级结果不缓存，让用户可以立即重试；完整结果才用于削峰。
        if not snapshot_is_partial(data):
            _CACHE_PAYLOAD = copy.deepcopy(data)
            _CACHE_AT = time.monotonic()
        return copy.deepcopy(data), False


def _showstart_counts(data):
    status = (data.get("source_status") or {}).get("showstart") or {}
    return int(status.get("ok") or 0), int(status.get("fail") or 0)


def snapshot_is_partial(data):
    """有任一秀动采集器降级时，不把结果当成可缓存的完整快照。"""
    _, failed = _showstart_counts(data)
    return failed > 0 or bool(data.get("notes"))


def request_is_allowed(headers):
    """只允许同源浏览器请求；没有浏览器来源头的服务端请求仍可使用。"""
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
        return False

    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (headers.get("Host") or "").strip().lower()
    return bool(host) and parsed.netloc.lower() == host


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload, cache=False):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header(
                "Cache-Control",
                "public, max-age=0, s-maxage=60, stale-while-revalidate=30",
            )
            self.send_header("CDN-Cache-Control", "public, s-maxage=60")
            self.send_header("Vercel-CDN-Cache-Control", "public, s-maxage=60")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _refresh(self):
        if not request_is_allowed(self.headers):
            self._send_json(403, {"ok": False, "error": "已拒绝跨站刷新请求"})
            return
        try:
            data, cached = refresh_snapshot()
        except Exception as exc:
            self._send_json(502, {
                "ok": False,
                "error": "刷新失败，请稍后重试",
                "detail": "%s: %s" % (type(exc).__name__, exc),
            })
            return
        complete, failed = _showstart_counts(data)
        partial = snapshot_is_partial(data)
        if failed and not complete:
            self._send_json(502, {
                "ok": False,
                "partial": True,
                "error": "秀动暂时未返回有效结果，当前页面数据未受影响",
            })
            return
        self._send_json(200, {
            "ok": True,
            "cached": cached,
            "partial": partial,
            "data": data,
        }, cache=not partial)

    def do_GET(self):
        self._refresh()

    def do_POST(self):
        self._refresh()

    def do_OPTIONS(self):
        if not request_is_allowed(self.headers):
            self._send_json(403, {"ok": False, "error": "已拒绝跨站刷新请求"})
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
