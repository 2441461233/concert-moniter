"""演出/舆情数据的合并、去重、状态判定与变更追踪。

设计要点：
- 同一场演出可能同时出现在秀动和大麦，用 (艺人, 日期, 城市) 做指纹跨源合并，
  每条记录保留所有来源链接。
- first_seen 一旦写入永不改动 —— 前端"新增"角标和变更日志都依赖它。
- 状态只有三种对外形态：on_sale(在售) / upcoming(已官宣待开票) / ended(已结束)。
"""
import hashlib
import json
import os
import re
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
RUMORS_PATH = os.path.join(DATA_DIR, "rumors.json")
META_PATH = os.path.join(DATA_DIR, "meta.json")
CHANGELOG_PATH = os.path.join(DATA_DIR, "changes.log")

# 售罄不等于结束：票卖光但还没演的场次仍然要留在时间线上 —— 还有缺货登记、
# 加场、二次放票的可能，而且你得知道这场自己没抢到。只有日期已过或明确标记
# 演出结束的，才算 ended。
ON_SALE_STATES = {"on_sale", "预售中", "selling", "sold_out", "售罄"}
ENDED_STATES = {"ended", "已结束"}
SOLD_OUT_STATES = {"sold_out", "售罄"}


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _norm(s):
    return re.sub(r"[\s\-·|｜/【】\[\]（）()]+", "", (s or "")).lower()


def fingerprint(ev):
    """跨源去重键：同艺人 + 同日期 + 同城市 视为同一场。"""
    parts = [ev.get("artist_key", ""), ev.get("show_date", "")]
    locus = _norm(ev.get("city")) or _norm(ev.get("venue")) or _norm(ev.get("title"))[:16]
    parts.append(locus)
    if not ev.get("show_date"):
        # 没有日期的（多为"官宣待定"），退化成按标题区分，避免全部挤成一条
        parts.append(_norm(ev.get("title"))[:24])
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def derive_status(ev):
    """归一化成前端用的三态。已过期的一律 ended。"""
    date = ev.get("show_date", "")
    if date and date < today():
        return "ended"
    raw = (ev.get("sale_status") or "").strip()
    if raw in ENDED_STATES:
        return "ended"
    if raw in ON_SALE_STATES:
        return "on_sale"
    if raw in ("upcoming", "announced", "paused", "待开票", "即将开售"):
        return "upcoming"
    sale_time = ev.get("sale_time", "")
    if sale_time:
        return "on_sale" if sale_time[:10] <= today() else "upcoming"
    # 状态不明：有日期就当已官宣待定，没日期也一样
    return "upcoming"


# 调研补录的字段优先级低于采集器，但空值不覆盖非空值
_MERGE_KEEP_RICHER = [
    "title", "performers", "city", "venue", "show_date", "show_time",
    "show_time_raw", "price", "sale_time", "note", "tour_name",
]


def _merge_one(old, new):
    """new 覆盖 old，但只在 new 该字段非空时覆盖。"""
    out = dict(old)
    for k in _MERGE_KEEP_RICHER:
        v = new.get(k)
        if v:
            out[k] = v
    for k in ("ticket_tiers", "sources"):
        merged = list(old.get(k) or [])
        for item in (new.get(k) or []):
            if item not in merged:
                merged.append(item)
        out[k] = merged
    # 状态取"更确定"的那个：采集器 confirmed 优于调研 rumor
    if new.get("confidence") == "confirmed" or not old.get("confidence"):
        out["confidence"] = new.get("confidence", old.get("confidence", "confirmed"))
    if new.get("sale_status"):
        out["sale_status"] = new["sale_status"]
    out["artist_key"] = new.get("artist_key") or old.get("artist_key")
    out["artist_name"] = new.get("artist_name") or old.get("artist_name")
    return out


def normalize_event(ev):
    src = ev.get("source", "unknown")
    out = {
        "source": src,
        "artist_key": ev.get("artist_key", ""),
        "artist_name": ev.get("artist_name", ""),
        "title": (ev.get("title") or "").strip(),
        "tour_name": (ev.get("tour_name") or "").strip(),
        "performers": (ev.get("performers") or "").strip(),
        "city": (ev.get("city") or "").strip(),
        "venue": (ev.get("venue") or "").strip(),
        "country": (ev.get("country") or "").strip(),
        "show_date": (ev.get("show_date") or "").strip(),
        "show_time": (ev.get("show_time") or "").strip(),
        "show_time_raw": (ev.get("show_time_raw") or "").strip(),
        "price": (ev.get("price") or "").strip(),
        "ticket_tiers": ev.get("ticket_tiers") or [],
        "sale_status": (ev.get("sale_status") or "").strip(),
        "sale_time": (ev.get("sale_time") or "").strip(),
        "confidence": ev.get("confidence") or "confirmed",
        "note": (ev.get("note") or "").strip(),
        "sources": [{
            "source": src,
            "url": ev.get("url", ""),
            "source_id": str(ev.get("source_id", "")),
        }],
    }
    return out


def merge_events(incoming, run_id):
    """把一批采集结果并入 events.json。返回变更列表。"""
    store = _load(EVENTS_PATH, {})
    changes = []
    stamp = now_iso()

    # 同一来源的同一场演出必须落到同一条记录上。指纹里含城市，而秀动会临时改
    # 场地/城市字段（实例：event 306791 的城市从「嘉兴」变成「杭州」），只靠指纹
    # 会把同一场裂成两条。source_id 是权威标识，优先用它定位。
    #
    # 但键必须带 artist_key：一个音乐节 source_id 只有一个，却会因为多位关注艺人
    # 同台而产生多条记录（沙一汀和加木同时出现在禧都济南站）。只按 source_id 归并
    # 会把它们压成一条，丢掉艺人归属。
    by_source_id = {}
    for key, rec in store.items():
        for s in rec.get("sources", []):
            if s.get("source") and s.get("source_id"):
                by_source_id[(s["source"], str(s["source_id"]), rec.get("artist_key", ""))] = key

    for raw in incoming:
        ev = normalize_event(raw)
        if not ev["title"] and not ev["show_date"]:
            continue
        fp = fingerprint(ev)
        src = ev["sources"][0]
        src_key = ((src.get("source"), src.get("source_id"), ev["artist_key"])
                   if src.get("source_id") else None)
        if src_key and src_key in by_source_id:
            fp = by_source_id[src_key]
        elif src_key:
            by_source_id[src_key] = fp
        if fp in store:
            before = store[fp]
            prev_status = derive_status(before)
            prev_sale_time = before.get("sale_time", "")
            merged = _merge_one(before, ev)
            merged["first_seen"] = before.get("first_seen", stamp)
            merged["first_seen_run"] = before.get("first_seen_run", run_id)
            merged["last_seen"] = stamp
            merged["id"] = fp
            store[fp] = merged

            new_status = derive_status(merged)
            if new_status != prev_status:
                changes.append({
                    "kind": "status", "id": fp, "artist": merged["artist_name"],
                    "title": merged["title"],
                    "detail": "%s → %s" % (prev_status, new_status),
                })
            if merged.get("sale_time") and merged["sale_time"] != prev_sale_time:
                changes.append({
                    "kind": "sale_time", "id": fp, "artist": merged["artist_name"],
                    "title": merged["title"],
                    "detail": "开票时间：%s" % merged["sale_time"],
                })
        else:
            ev["first_seen"] = stamp
            ev["first_seen_run"] = run_id
            ev["last_seen"] = stamp
            ev["id"] = fp
            store[fp] = ev
            # 首次运行会回填大量历史场次，那不是"新消息"，不该进变更日志
            if derive_status(ev) != "ended":
                changes.append({
                    "kind": "new", "id": fp, "artist": ev["artist_name"],
                    "title": ev["title"],
                    "detail": "%s %s %s" % (ev["show_date"], ev["city"],
                                            ev["sale_status"] or ""),
                })

    _save(EVENTS_PATH, store)
    return changes


def rumor_fingerprint(r):
    base = _norm(r.get("artist_key")) + _norm(r.get("headline"))[:40]
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def merge_rumors(incoming, run_id):
    store = _load(RUMORS_PATH, {})
    changes = []
    stamp = now_iso()
    for r in incoming:
        headline = (r.get("headline") or "").strip()
        if not headline:
            continue
        fp = rumor_fingerprint(r)
        rec = {
            "id": fp,
            "artist_key": r.get("artist_key", ""),
            "artist_name": r.get("artist_name", ""),
            "headline": headline,
            "detail": (r.get("detail") or "").strip(),
            "source_name": (r.get("source_name") or "").strip(),
            "url": (r.get("url") or "").strip(),
            "credibility": r.get("credibility") or "low",
            "posted_at": (r.get("posted_at") or "").strip(),
        }
        if fp in store:
            old = store[fp]
            rec["first_seen"] = old.get("first_seen", stamp)
            rec["first_seen_run"] = old.get("first_seen_run", run_id)
        else:
            rec["first_seen"] = stamp
            rec["first_seen_run"] = run_id
            changes.append({
                "kind": "rumor", "id": fp, "artist": rec["artist_name"],
                "title": headline, "detail": rec["source_name"],
            })
        rec["last_seen"] = stamp
        store[fp] = rec
    _save(RUMORS_PATH, store)
    return changes


def load_events():
    return _load(EVENTS_PATH, {})


def load_rumors():
    return _load(RUMORS_PATH, {})


def load_meta():
    return _load(META_PATH, {"runs": [], "last_run": None})


def save_meta(meta):
    _save(META_PATH, meta)


def append_changelog(run_id, changes):
    if not changes:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CHANGELOG_PATH, "a", encoding="utf-8") as f:
        for c in changes:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (
                run_id, c["kind"], c.get("artist", ""), c.get("title", ""),
                c.get("detail", "")))
