"""极简 HTTP 拉取 + 磁盘缓存。只用标准库。"""
import gzip
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", ".cache")


def get(url, params=None, timeout=20, cache_ttl=0, referer=None):
    """返回 (text, error)。失败时 text 为 None，error 是可读原因。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = os.path.join(CACHE_DIR, key + ".html")
    if cache_ttl > 0 and os.path.exists(path):
        if time.time() - os.path.getmtime(path) < cache_ttl:
            with open(path, encoding="utf-8") as f:
                return f.read(), None

    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
        "Referer": referer or url,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:  # 超时 / DNS / TLS
        return None, "%s: %s" % (type(e).__name__, e)

    if cache_ttl > 0:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return text, None
