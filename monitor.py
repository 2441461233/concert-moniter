#!/usr/bin/env python3
"""演唱会监控器 —— 命令行入口。

用法：
    python3 monitor.py check            # 跑一次采集（秀动）→ 合并 → 重建站点
    python3 monitor.py ingest <file>    # 并入一份调研 JSON（大麦/KPop/舆情）
    python3 monitor.py build            # 只重建 site/data.js
    python3 monitor.py status           # 打印当前库存概览
    python3 monitor.py prune [--days N] # 清掉 N 天前已结束的场次（默认 60）

依赖：只有 Python 3 标准库。
"""
import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import showstart, store  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config", "artists.json")

# 舆情线索在站点上的存活天数。超过则从站点隐藏（数据仍留在 data/rumors.json，
# 要彻底删除用 prune）。调这个数就能改变舆情流的保留窗口。
RUMOR_TTL_DAYS = 90
SITE_DIR = os.path.join(ROOT, "site")
INBOX_DIR = os.path.join(ROOT, "research", "inbox")
ARCHIVE_DIR = os.path.join(ROOT, "research", "archive")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def enabled_artists(cfg):
    return [a for a in cfg["artists"] if a.get("enabled", True)]


# ---------------------------------------------------------------- check

def cmd_check(args):
    cfg = load_config()
    artists = enabled_artists(cfg)
    run_id = store.local_now().strftime("%Y%m%d-%H%M%S")
    print("[%s] 开始检查，%d 位艺人" % (run_id, len(artists)))

    all_events, all_notes = [], []
    source_status = {"showstart": {"ok": 0, "fail": 0}}
    config_dirty = False

    collect_artists = []
    for a in artists:
        if a.get("region") == "kpop" and not a.get("showstart_artist_id"):
            # 秀动基本没有 KPop 团体，跳过省时间；仍由调研环节覆盖
            print("  · %-14s 跳过秀动（海外艺人，走调研补全）" % a["name"])
            continue
        collect_artists.append(a)

    def collect_one(artist):
        return showstart.collect(
            artist,
            cache_ttl=0 if getattr(args, "force", False) else 1800,
            sleep=getattr(args, "sleep", 0.4),
        )

    workers = max(1, min(
        int(getattr(args, "concurrent_workers", 1) or 1),
        len(collect_artists) or 1,
    ))
    if workers == 1:
        collected = []
        for a in collect_artists:
            try:
                collected.append((a, collect_one(a), None))
            except Exception as exc:
                collected.append((a, None, exc))
    else:
        collected = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [(a, executor.submit(collect_one, a)) for a in collect_artists]
            for a, future in futures:
                try:
                    collected.append((a, future.result(), None))
                except Exception as exc:
                    collected.append((a, None, exc))

    for a, result, collect_error in collected:
        if collect_error is not None:
            all_notes.append("%s 秀动采集异常: %s: %s" % (
                a["name"], type(collect_error).__name__, collect_error))
            source_status["showstart"]["fail"] += 1
            print("  · %-14s 异常：%s" % (a["name"], collect_error))
            continue
        try:
            events, notes, discovered = result
        except Exception as e:  # 单个艺人失败不应该拖垮整轮
            all_notes.append("%s 秀动采集异常: %s: %s" % (a["name"], type(e).__name__, e))
            source_status["showstart"]["fail"] += 1
            print("  · %-14s 异常：%s" % (a["name"], e))
            continue

        if discovered and not a.get("showstart_artist_id"):
            a["showstart_artist_id"] = discovered
            config_dirty = True
            print("  · %-14s 自动发现秀动艺人 ID %s" % (a["name"], discovered))

        all_events.extend(events)
        all_notes.extend(notes)
        source_status["showstart"]["fail" if notes else "ok"] += 1
        print("  · %-14s 秀动 %d 场" % (a["name"], len(events)))

    # 完整刷新要求原子性：任一应采的秀动艺人降级时，在这里
    # 就中止，不发现/写回艺人 ID，不移动 inbox，也不合并任何数据。
    # 日常单项 check 保持原有的降级沿用行为。
    if getattr(args, "strict_sources", False) and source_status["showstart"]["fail"]:
        raise RuntimeError("秀动采集未完整，本轮未写入任何数据")

    if config_dirty:
        save_config(cfg)

    # 自动并入 research/inbox 里还没处理的调研文件
    if getattr(args, "no_inbox", False):
        ingested, research_changes = [], []
    else:
        ingested, research_changes = _ingest_inbox(run_id)

    changes = store.merge_events(all_events, run_id)
    store.append_changelog(run_id, changes)
    changes += research_changes

    meta = store.load_meta()
    meta["last_run"] = store.now_iso()
    meta["last_run_id"] = run_id
    meta["last_data_run"] = run_id
    if ingested:
        meta["last_research_at"] = store.now_iso()
    meta["notes"] = all_notes
    meta["source_status"] = source_status
    meta.setdefault("runs", []).append({
        "run_id": run_id, "at": store.now_iso(),
        "events_seen": len(all_events), "changes": len(changes),
        "ingested_files": ingested,
    })
    meta["runs"] = meta["runs"][-50:]
    store.save_meta(meta)

    build_site()

    print("\n本轮变更 %d 条：" % len(changes))
    for c in changes[:30]:
        print("  [%s] %s — %s %s" % (c["kind"], c.get("artist", ""),
                                     c.get("title", ""), c.get("detail", "")))
    if all_notes:
        print("\n降级/失败提示：")
        for n in all_notes:
            print("  ! " + n)
    print("\n站点已更新：%s" % os.path.join(SITE_DIR, "index.html"))


# ---------------------------------------------------------------- ingest

def _ingest_one(path, run_id):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    events = payload.get("events", [])
    rumors = payload.get("rumors", [])
    changes = store.merge_events(events, run_id)
    changes += store.merge_rumors(rumors, run_id)
    store.append_changelog(run_id, changes)
    return len(events), len(rumors), changes


def _ingest_inbox(run_id):
    """返回 (已处理文件名, 调研带来的变更)。"""
    if not os.path.isdir(INBOX_DIR):
        return [], []
    done, changes = [], []
    for name in sorted(os.listdir(INBOX_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(INBOX_DIR, name)
        try:
            ne, nr, ch = _ingest_one(path, run_id)
        except Exception as e:
            print("  ! 调研文件 %s 解析失败：%s" % (name, e))
            continue
        print("  · 并入调研 %s：演出 %d / 舆情 %d，变更 %d" % (name, ne, nr, len(ch)))
        changes.extend(ch)
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        os.replace(path, os.path.join(ARCHIVE_DIR, "%s-%s" % (run_id, name)))
        done.append(name)
    return done, changes


def cmd_ingest(args):
    run_id = store.local_now().strftime("%Y%m%d-%H%M%S")
    ne, nr, changes = _ingest_one(args.file, run_id)
    print("并入演出 %d 条 / 舆情 %d 条，产生 %d 条变更" % (ne, nr, len(changes)))
    for c in changes[:30]:
        print("  [%s] %s — %s %s" % (c["kind"], c.get("artist", ""),
                                     c.get("title", ""), c.get("detail", "")))
    meta = store.load_meta()
    meta["last_research_at"] = store.now_iso()
    # 单独 ingest 也是一次「动过数据的运行」，本轮变化要能反映它
    meta["last_data_run"] = run_id
    store.save_meta(meta)
    build_site()
    print("站点已更新。")


# ---------------------------------------------------------------- build

def _sort_key_show(e):
    return (e.get("show_date") or "9999-99-99", e.get("show_time") or "")


def _sort_key_sale(e):
    return (e.get("sale_time") or "9999", e.get("show_date") or "9999-99-99")


def load_recent_changes(run_id, limit=40):
    """读取 changes.log 中属于指定 run 的变更，供站点「本轮变化」板块使用。

    必须按 run_id 精确过滤，不能取「日志里最后一个 run」：append_changelog 在
    无变更时根本不写入，取最后一个 run 会把上一次有变化的旧记录当成本轮结果，
    而且永远不会归零 —— 那样每天看到的都是同一批「新增」。
    """
    if not run_id:
        return []
    path = store.CHANGELOG_PATH
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 4 and parts[0] == run_id:
                rows.append({
                    "run": parts[0], "type": parts[1],
                    "artist": parts[2], "title": parts[3],
                    "info": parts[4] if len(parts) > 4 else "",
                })
    return rows[:limit]


def rumor_freshness(r):
    """舆情的新鲜度日期 = max(消息发布时间, 最近一次被调研报到的时间)。

    只看 posted_at 会误杀仍然有效的线索：门尼「巡演已官宣但城市未公布」这类，
    只要巡演一天没公布就一天有效。只要每日调研还在持续报到它，last_seen 就会刷新，
    它就一直算活跃；调研不再提它了，才开始计龄。

    posted_at 精度参差（可能只有 '2026' 或 '2026-07'），按该区间最早一天保守解析。
    """
    posted = (r.get("posted_at") or "").strip()
    if len(posted) == 4:            # 2026
        posted += "-01-01"
    elif len(posted) == 7:          # 2026-07
        posted += "-01"
    seen = (r.get("last_seen") or r.get("first_seen") or "")[:10]
    return max(posted[:10], seen)


def build_site():
    cfg = load_config()
    events = store.load_events()
    rumors = store.load_rumors()
    meta = store.load_meta()
    # is_new 和 changes 必须来自同一个 run，否则前端两处会自相矛盾。
    # last_data_run 由 check 和 ingest 共同维护，代表「最近一次真正动过数据的运行」。
    last_run = meta.get("last_data_run") or meta.get("last_run_id", "")

    on_sale, upcoming, ended = [], [], []
    for ev in events.values():
        item = dict(ev)
        item["status"] = store.derive_status(ev)
        item["is_new"] = bool(last_run) and ev.get("first_seen_run") == last_run
        {"on_sale": on_sale, "upcoming": upcoming, "ended": ended}[item["status"]].append(item)

    on_sale.sort(key=_sort_key_show)
    upcoming.sort(key=_sort_key_sale)
    ended.sort(key=_sort_key_show, reverse=True)

    rumor_cutoff = (store.local_now() - timedelta(days=RUMOR_TTL_DAYS)).strftime("%Y-%m-%d")
    fresh_rumors = [r for r in rumors.values() if rumor_freshness(r) >= rumor_cutoff]
    aged_out = len(rumors) - len(fresh_rumors)
    rumor_list = sorted(
        ({**r, "is_new": bool(last_run) and r.get("first_seen_run") == last_run}
         for r in fresh_rumors),
        key=lambda r: (rumor_freshness(r), r.get("first_seen") or ""),
        reverse=True,
    )

    data = {
        "generated_at": store.now_iso(),
        "last_run": meta.get("last_run"),
        "last_run_id": last_run,
        "last_research_at": meta.get("last_research_at"),
        # 只有「全部 enabled 艺人 + 全信息源」流程完成后才更新；
        # 单独采集秀动或普通 ingest 都不会改动这个时间。
        "full_refresh_at": meta.get("full_refresh_at"),
        "full_refresh_id": meta.get("full_refresh_id"),
        "full_refresh_status": meta.get("full_refresh_status"),
        "artists": [{"key": a["key"], "name": a["name"], "region": a.get("region", "cn")}
                    for a in cfg["artists"] if a.get("enabled", True)],
        "on_sale": on_sale,
        "upcoming": upcoming,
        "ended": ended[:40],
        "rumors": rumor_list,
        "changes": load_recent_changes(last_run),
        "notes": meta.get("notes", []),
        "source_status": meta.get("source_status", {}),
        "counts": {"on_sale": len(on_sale), "upcoming": len(upcoming),
                   "rumors": len(rumor_list), "ended": len(ended),
                   "rumors_aged_out": aged_out},
    }

    os.makedirs(SITE_DIR, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=1)
    _atomic_write_text(
        os.path.join(SITE_DIR, "data.js"),
        "window.__CM_DATA__ = " + serialized + ";\n",
    )
    # 同时留一份纯 JSON，方便以后接别的客户端
    _atomic_write_text(os.path.join(SITE_DIR, "data.json"), serialized)
    return data


def _atomic_write_text(path, content):
    """在目标目录内写临时文件，再原子替换，避免客户端读到半份 JSON。"""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=os.path.dirname(path),
                prefix=".%s." % os.path.basename(path), delete=False) as f:
            tmp_path = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def cmd_build(args):
    data = build_site()
    print("已生成 site/data.js —— 在售 %d / 待开票 %d / 舆情 %d" % (
        data["counts"]["on_sale"], data["counts"]["upcoming"], data["counts"]["rumors"]))


# ---------------------------------------------------------------- status

def cmd_status(args):
    data = build_site()
    meta = store.load_meta()
    print("最近一次检查：%s (%s)" % (meta.get("last_run", "从未"), meta.get("last_run_id", "-")))
    print("最近一次调研：%s" % meta.get("last_research_at", "从未"))
    aged = data["counts"].get("rumors_aged_out", 0)
    print("在售 %d ｜ 已官宣待开票 %d ｜ 舆情 %d ｜ 已结束 %d%s\n" % (
        data["counts"]["on_sale"], data["counts"]["upcoming"],
        data["counts"]["rumors"], data["counts"]["ended"],
        "（另有 %d 条舆情超过 %d 天已从站点隐藏）" % (aged, RUMOR_TTL_DAYS) if aged else ""))
    for title, key in (("正在售卖", "on_sale"), ("已官宣 / 待开票", "upcoming")):
        print("── %s ──" % title)
        for e in data[key][:25]:
            flag = "🆕" if e.get("is_new") else "  "
            when = e.get("show_date") or "待定"
            sale = ("｜开票 " + e["sale_time"]) if e.get("sale_time") else ""
            print("%s %s  %-6s %-12s %s%s" % (
                flag, when, e.get("artist_name", ""), e.get("city") or "-",
                (e.get("title") or "")[:40], sale))
        if not data[key]:
            print("  （空）")
        print()
    if data["rumors"]:
        print("── 舆情 ──")
        for r in data["rumors"][:15]:
            print("  [%s] %s — %s" % (r.get("credibility", "?"),
                                      r.get("artist_name", ""), r.get("headline", "")[:50]))


# ---------------------------------------------------------------- prune

def cmd_prune(args):
    cutoff = (store.local_now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    events = store.load_events()
    drop = [k for k, e in events.items()
            if e.get("show_date") and e["show_date"] < cutoff]
    for k in drop:
        del events[k]
    store._save(store.EVENTS_PATH, events)

    # 舆情同样需要清理，否则只进不出，一年后整个模块会变成垃圾场
    rumors = store.load_rumors()
    rdrop = [k for k, r in rumors.items() if rumor_freshness(r) < cutoff]
    for k in rdrop:
        del rumors[k]
    store._save(store.RUMORS_PATH, rumors)

    build_site()
    print("清理了 %d 场演出、%d 条舆情（%s 之前）。" % (len(drop), len(rdrop), cutoff))


def main():
    p = argparse.ArgumentParser(description="演唱会监控器")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("check", help="跑一次采集并重建站点")
    pc.add_argument("--force", action="store_true", help="忽略秀动 HTTP 缓存")
    pc.add_argument("--sleep", type=float, default=0.4,
                    help="秀动请求间隔秒数（默认 0.4）")
    pc.add_argument("--concurrent-workers", type=int, default=1,
                    help="秀动艺人并发数（默认 1）")
    pc.add_argument("--strict-sources", action="store_true",
                    help="任一应采秀动源降级时在合并前中止")
    pc.add_argument("--no-inbox", action="store_true",
                    help="不处理 research/inbox（适合只读部署环境）")
    pc.set_defaults(func=cmd_check)

    pi = sub.add_parser("ingest", help="并入一份调研 JSON")
    pi.add_argument("file")
    pi.set_defaults(func=cmd_ingest)

    sub.add_parser("build", help="只重建站点数据").set_defaults(func=cmd_build)
    sub.add_parser("status", help="打印概览").set_defaults(func=cmd_status)

    pp = sub.add_parser("prune", help="清理过期场次")
    pp.add_argument("--days", type=int, default=60)
    pp.set_defaults(func=cmd_prune)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
