"""秀动（showstart.com）采集器。

秀动的搜索页和艺人页都是服务端渲染的，直接解析 HTML 即可，不需要浏览器。
两处的 DOM 类名不同，用一个通用的字段抽取器同时兼容。

秀动搜索是模糊的（搜"门尼"会返回不含门尼的演出），所以必须用别名做严格二次过滤。
"""
import re
import time
from datetime import datetime

from . import http

BASE = "https://www.showstart.com"
SEARCH_URL = BASE + "/event/list"
ARTIST_URL = BASE + "/artist/%s"
EVENT_URL = BASE + "/event/%s"

# 搜索页：<a href="/event/303752" class="show-item item" ...> ... </a>
# 艺人页：<div class="table-cell"><a href="/event/303752" data-v-xxx> ... </a>
_ITEM_RE = re.compile(
    r'<a\s+href="/event/(\d+)"[^>]*>(.*?)</a>', re.S)


def _strip(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _field(block, *class_names):
    """按 class 名抽字段，兼容 div/p/span 和搜索页/艺人页两套类名。"""
    for name in class_names:
        m = re.search(
            r'<(?:div|p|span)\s+class="%s"[^>]*>(.*?)</(?:div|p|span)>' % name,
            block, re.S)
        if m:
            return _strip(m.group(1))
    return ""


def _parse_time(raw):
    """'时间：2026/07/18 21:00' / '2026/07/18 21:00' -> ('2026-07-18', '21:00')"""
    raw = raw.replace("时间：", "").strip()
    m = re.search(r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})", raw)
    if not m:
        return "", ""
    date = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    t = re.search(r"(\d{1,2}):(\d{2})", raw)
    return date, ("%02d:%s" % (int(t.group(1)), t.group(2))) if t else ""


def _parse_addr(raw):
    """'[北京]北京欢乐谷' -> ('北京', '北京欢乐谷')；艺人页只有场馆名。"""
    raw = raw.strip()
    m = re.match(r"\[(.+?)\]\s*(.*)", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", raw


def _parse_items(html):
    out = []
    for eid, block in _ITEM_RE.findall(html):
        title = _field(block, "title", "name")
        if not title:
            continue  # 导航链接之类，没有标题就不是演出卡片
        performers = _field(block, "artist", "performerName").replace("艺人：", "")
        price = _field(block, "price").replace("价格：", "")
        date, clock = _parse_time(_field(block, "time"))
        city, venue = _parse_addr(_field(block, "addr"))
        out.append({
            "event_id": eid,
            "title": title,
            "performers": performers,
            "price": price,
            "show_date": date,
            "show_time": clock,
            "city": city,
            "venue": venue,
        })
    return out


def _matches(item, aliases):
    """严格判定：别名必须出现在艺人栏或标题里。秀动搜索模糊，这一步是必须的。"""
    hay = (item.get("performers", "") + " " + item.get("title", "")).lower()
    return any(a.lower() in hay for a in aliases if a.strip())


SALE_HINTS = [
    ("售罄", "sold_out"),
    ("暂停销售", "paused"),
    ("即将开售", "upcoming"),
    ("即将开抢", "upcoming"),
    ("预售中", "on_sale"),
    ("开售时间", "upcoming"),
    ("演出结束", "ended"),
]


def fetch_detail(event_id, cache_ttl=3600):
    """补齐城市/场馆/票价档位/开售状态，并顺便发现艺人 ID。"""
    html, err = http.get(EVENT_URL % event_id, cache_ttl=cache_ttl)
    if err:
        return {}, err
    text = _strip(re.sub(r"<script.*?</script>", " ", html, flags=re.S))
    out = {}

    m = re.search(r"演出时间：(.+?)\s*艺人：", text)
    if m:
        out["time_raw"] = m.group(1).strip()
    m = re.search(r"场地：\s*(\S+)\s+(.+?)\s*地址：", text)
    if m:
        out["city"] = m.group(1).strip()
        out["venue"] = m.group(2).strip()

    tiers = re.findall(r"￥(\d+)\s+(\S+?票)", text)
    if tiers:
        out["ticket_tiers"] = ["¥%s %s" % (p, n) for p, n in tiers]

    for needle, status in SALE_HINTS:
        if needle in text:
            out["sale_status"] = status
            break

    # 艺人页链接：<a href="/artist/4501841">沙一汀EL</a>
    out["artist_links"] = [
        {"id": i, "name": _strip(n)}
        for i, n in re.findall(r'<a\s+href="/artist/(\d+)"[^>]*>(.*?)</a>', html, re.S)
        if _strip(n)
    ]
    return out, None


def collect(artist, fetch_details=True, cache_ttl=1800, sleep=0.4):
    """采集单个艺人。返回 (events, notes)。notes 记录失败/降级情况。"""
    aliases = [a for a in artist.get("aliases", []) if a.strip()] or [artist["name"]]
    notes = []
    raw = {}

    # 1) 艺人页 —— 最准，但需要先知道 artist id
    aid = (artist.get("showstart_artist_id") or "").strip()
    if aid:
        html, err = http.get(ARTIST_URL % aid, cache_ttl=cache_ttl)
        if err:
            notes.append("秀动艺人页 %s 拉取失败: %s" % (aid, err))
        else:
            for it in _parse_items(html):
                raw[it["event_id"]] = it

    # 2) 关键词搜索（全国，cityCode 留空）—— 覆盖艺人页漏掉的拼盘/音乐节
    for term in {artist["name"]} | set(aliases):
        html, err = http.get(SEARCH_URL, params={"keyword": term, "cityCode": ""},
                             cache_ttl=cache_ttl)
        if err:
            notes.append("秀动搜索「%s」失败: %s" % (term, err))
            continue
        for it in _parse_items(html):
            if _matches(it, aliases):
                raw.setdefault(it["event_id"], it)
        time.sleep(sleep)

    today = datetime.now().strftime("%Y-%m-%d")
    events, discovered_id = [], None
    for eid, it in raw.items():
        detail = {}
        if fetch_details:
            detail, err = fetch_detail(eid, cache_ttl=cache_ttl)
            if err:
                notes.append("秀动详情页 %s 失败: %s" % (eid, err))
            time.sleep(sleep)
            for link in detail.get("artist_links", []):
                if not aid and any(a.lower() == link["name"].lower() for a in aliases):
                    discovered_id = link["id"]

        date = it["show_date"]
        status = detail.get("sale_status")
        if not status:
            status = "on_sale" if date and date >= today else "ended"
        elif status == "ended":
            pass
        if date and date < today:
            status = "ended"

        events.append({
            "source": "showstart",
            "source_id": eid,
            "url": EVENT_URL % eid,
            "artist_key": artist["key"],
            "artist_name": artist["name"],
            "title": it["title"],
            "performers": it["performers"],
            "city": detail.get("city") or it["city"],
            "venue": detail.get("venue") or it["venue"],
            "show_date": date,
            "show_time": it["show_time"],
            "show_time_raw": detail.get("time_raw", ""),
            "price": it["price"],
            "ticket_tiers": detail.get("ticket_tiers", []),
            "sale_status": status,
            "sale_time": "",          # 秀动 SSR 不吐开售时间，交给调研环节补
            "confidence": "confirmed",
            "note": "",
        })

    return events, notes, discovered_id
