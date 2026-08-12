"""触发并查询 GitHub Actions 的完整演唱会数据刷新任务。

Vercel 只负责很薄的一层调度；耗时的秀动采集、全部艺人联网调研、数据合并和
Git 提交都在 GitHub Actions 中执行。这样浏览器关闭后任务仍会继续，刷新结果也
会成为所有访客共享的下一份生产快照。
"""

import json
import hmac
import hashlib
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit


GITHUB_API = "https://api.github.com"
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "2441461233/concert-moniter")
WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "full-refresh.yml")
GITHUB_TOKEN_ENV = "GITHUB_ACTIONS_TOKEN"
REFRESH_SECRET_ENV = "REFRESH_SECRET"
ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
JOB_ID_RE = re.compile(r"^[a-f0-9]{24}$")


class RefreshConfigurationError(RuntimeError):
    pass


def _github_request(method, path, payload=None):
    token = (os.environ.get(GITHUB_TOKEN_ENV) or "").strip()
    if not token:
        raise RefreshConfigurationError(
            "完整刷新尚未配置：缺少 Vercel 环境变量 %s" % GITHUB_TOKEN_ENV
        )
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "User-Agent": "concert-moniter-refresh",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        GITHUB_API + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = error_payload.get("message") or ""
        except (ValueError, OSError):
            pass
        raise RuntimeError("GitHub API HTTP %s%s" % (
            exc.code, (": " + detail) if detail else ""
        )) from exc


def _workflow_path(suffix=""):
    repository = urllib.parse.quote(REPOSITORY, safe="/")
    workflow = urllib.parse.quote(WORKFLOW_FILE, safe="")
    return "/repos/%s/actions/workflows/%s%s" % (repository, workflow, suffix)


def list_recent_runs(limit=30):
    payload = _github_request(
        "GET", _workflow_path(
            "/runs?event=workflow_dispatch&branch=main&per_page=%d" % limit
        )
    ) or {}
    return payload.get("workflow_runs") or []


def _job_id_from_run(run):
    title = str(run.get("display_title") or run.get("name") or "")
    match = re.fullmatch(r"Full refresh · ([a-f0-9]{24})", title)
    return match.group(1) if match else ""


def _find_run(runs, job_id):
    for run in runs:
        if _job_id_from_run(run) == job_id:
            return run
    return None


def _active_run(runs):
    # 只复用由这个页面发起、且能恢复追踪编号的任务。仓库管理员
    # 手动运行过旧版 workflow 时，不应返回空 job_id 让前端卡住。
    return next((
        run for run in runs
        if run.get("status") in ACTIVE_STATUSES and _job_id_from_run(run)
    ), None)


def dispatch_full_refresh(runs=None):
    runs = list_recent_runs() if runs is None else runs
    active = _active_run(runs)
    if active:
        return {
            "job_id": _job_id_from_run(active),
            "run_id": active.get("id"),
            "workflow_url": active.get("html_url"),
            "status": _public_status(active),
            "already_running": True,
        }

    job_id = secrets.token_hex(12)
    dispatch = _github_request("POST", _workflow_path("/dispatches"), {
        "ref": "main",
        "inputs": {"request_id": job_id},
    }) or {}
    return {
        "job_id": job_id,
        "run_id": dispatch.get("workflow_run_id"),
        "workflow_url": dispatch.get("html_url"),
        "status": "queued",
        "already_running": False,
    }


def _public_status(run):
    status = run.get("status")
    if status != "completed":
        return "in_progress" if status == "in_progress" else "queued"
    return "completed" if run.get("conclusion") == "success" else "failed"


def run_status(job_id, runs=None):
    if not JOB_ID_RE.fullmatch(job_id or ""):
        raise ValueError("任务编号格式无效")
    runs = list_recent_runs() if runs is None else runs
    run = _find_run(runs, job_id)
    if not run:
        # workflow_dispatch 返回 204 后，GitHub 通常需要数秒才把 run 放进列表。
        return {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "message": "完整刷新任务正在进入队列",
        }

    status = _public_status(run)
    messages = {
        "queued": ("queued", "完整刷新任务正在排队"),
        "in_progress": ("researching", "正在重新采集全部艺人与信息源"),
        "completed": ("publishing", "数据已更新，正在等待生产站点发布"),
        "failed": ("failed", "完整刷新失败，当前线上数据未受影响"),
    }
    stage, message = messages[status]
    result = {
        "job_id": job_id,
        "status": status,
        "stage": stage,
        "message": message,
        "started_at": run.get("run_started_at") or run.get("created_at"),
        "completed_at": run.get("updated_at") if run.get("status") == "completed" else None,
        "workflow_url": run.get("html_url"),
    }
    if status == "failed":
        result["error"] = "GitHub Actions: " + str(run.get("conclusion") or "failed")
    return result


def request_is_same_origin(headers, require_origin=False):
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
        return False
    origin = (headers.get("Origin") or "").strip()
    if not origin:
        return not require_origin
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    host = (headers.get("Host") or "").strip().lower()
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host


def request_can_trigger(headers):
    """完整刷新会消耗付费 API，只允许同源且持有刷新口令的请求。"""
    if not request_is_same_origin(headers, require_origin=True):
        return False
    expected = (os.environ.get(REFRESH_SECRET_ENV) or "").strip()
    if not expected:
        raise RefreshConfigurationError(
            "完整刷新尚未配置：缺少 Vercel 环境变量 %s" % REFRESH_SECRET_ENV
        )
    authorization = (headers.get("Authorization") or "").strip()
    supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def status_token_for(job_id):
    """为单个任务生成只读状态令牌。

    管理员口令仍然只存在当前标签页；localStorage 只保存这个无法
    用来触发新任务的派生令牌，既能跨刷新恢复跟踪，也不会把带 PAT
    的 GitHub runs 查询暴露成公开转发器。
    """
    if not JOB_ID_RE.fullmatch(job_id or ""):
        raise ValueError("任务编号格式无效")
    secret = (os.environ.get(REFRESH_SECRET_ENV) or "").strip()
    if not secret:
        raise RefreshConfigurationError(
            "完整刷新尚未配置：缺少 Vercel 环境变量 %s" % REFRESH_SECRET_ENV
        )
    return hmac.new(
        secret.encode("utf-8"),
        ("concert-monitor-status:" + job_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def request_can_read_status(job_id, supplied_token):
    try:
        expected = status_token_for(job_id)
    except ValueError:
        return False
    return bool(supplied_token) and hmac.compare_digest(supplied_token, expected)


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            if not request_can_trigger(self.headers):
                self._send_json(401, {"ok": False, "error": "刷新口令不正确"})
                return
            result = dispatch_full_refresh()
        except RefreshConfigurationError as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self._send_json(502, {
                "ok": False,
                "error": "无法启动完整刷新，请稍后重试",
            })
            return
        job_id = result.get("job_id")
        status_token = status_token_for(job_id)
        result.update({
            "ok": True,
            "status_url": "/api/refresh?" + urllib.parse.urlencode({
                "job_id": job_id or "", "status_token": status_token,
            }),
        })
        self._send_json(202, result)

    def do_GET(self):
        if not request_is_same_origin(self.headers):
            self._send_json(403, {"ok": False, "error": "已拒绝跨站状态请求"})
            return
        query = parse_qs(urlsplit(self.path).query)
        job_id = (query.get("job_id") or [""])[0]
        status_token = (query.get("status_token") or [""])[0]
        try:
            if not request_can_read_status(job_id, status_token):
                self._send_json(403, {"ok": False, "error": "任务状态令牌无效"})
                return
        except RefreshConfigurationError as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        try:
            result = run_status(job_id)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except RefreshConfigurationError as exc:
            self._send_json(503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self._send_json(502, {
                "ok": False,
                "error": "无法查询完整刷新状态",
            })
            return
        result["ok"] = True
        self._send_json(200, result)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
