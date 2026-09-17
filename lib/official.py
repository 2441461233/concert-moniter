"""Deterministic readers for explicitly configured, reviewed official notices.

No model output or search snippets enter this path. An empty/challenge/changed
page is a failure, never evidence that the artist has no shows.
"""
import re
from datetime import date, datetime
from html.parser import HTMLParser

from . import http


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts, self.links = [], []
        self.hidden = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "a":
            self.links.append(attrs.get("href", ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden-1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())

    @property
    def text(self):
        return "\n".join(self.parts)


def _korean_datetime(groups):
    year, month, day, hour, minute, period = groups
    hour = int(hour) % 12 + (12 if period == "PM" else 0)
    return datetime(int(year), int(month), int(day), hour, int(minute or 0))


KOREAN_DATETIME = (r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\([^)]*\)\s*"
                    r"(\d{1,2})(?::(\d{2}))?(AM|PM)\(KST\)")


def _korea(page):
    text = page.text.split("관련 공연\n［Play")[0]
    if not re.search(r"^ILLIT LIVE .+ ENCORE in KOREA$", text, re.M) or "인스파이어 아레나" not in text:
        raise ValueError("NOL ILLIT 安可公告结构或标题不匹配")
    schedule = re.search(r"운영 시간\s*(.*?)취소 및 환불", text, re.S)
    sale = re.search(r"일반예매\s*:\s*"+KOREAN_DATETIME, text)
    if not schedule or not sale:
        raise ValueError("NOL 演出或公售时间缺失")
    shows = re.findall(KOREAN_DATETIME, schedule.group(1))
    if len(shows) != 2 or len(shows) != len(set(shows)):
        raise ValueError("NOL 逐日演出时间缺失或重复")
    sale_time = _korean_datetime(sale.groups()).isoformat(timespec="minutes")+"+09:00"
    tickets = [u for u in page.links if re.fullmatch(r"https://world.nol.com/en/ticket/places/\d+/products/\d+", u)]
    tiers = re.findall(r"(SOUND CHECK|M&G|일반석)\s+([\d,]+)\s+원", text)
    if not tickets or len(tiers) != 3:
        raise ValueError("NOL 全球票务链接或票档缺失")
    result = []
    for match in shows:
        show = _korean_datetime(match)
        result.append({
            "title": "ILLIT LIVE 'PRESS START♥︎' ENCORE in KOREA · 仁川（首尔地区）",
            "tour_name": "ILLIT LIVE 'PRESS START♥︎' ENCORE",
            "city": "仁川（首尔地区）", "country": "韩国", "venue": "INSPIRE Arena",
            "show_date": show.date().isoformat(), "show_time": show.strftime("%H:%M"),
            "price": "KRW "+" / ".join(t[1] for t in tiers),
            "ticket_tiers": [label+" KRW "+price for label, price in tiers],
            "sale_status": "scheduled", "sale_time": sale_time,
            "note": "全球公售 %s（韩国时间，北京时间提前 1 小时）；开演为韩国时间，余票以票务页面为准。" % sale_time[:16].replace("T", " "),
            "ticket_url": tickets[0],
        })
    return result


def _japan(page):
    text = page.text
    if "ENCORE in JAPAN" not in text or "[千葉] LaLa arena TOKYO-BAY" not in text:
        raise ValueError("ILLIT 日本安可公告标题或场地不匹配")
    shows = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日\([^)]*\)\s*開場\s*(\d{1,2}:\d{2})／開演\s*(\d{1,2}:\d{2})", text)
    period = re.search(r"受付期間[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日\([^)]*\)(\d{1,2}:\d{2})[~～〜](\d{1,2})月(\d{1,2})日\([^)]*\)(\d{1,2}:\d{2})まで\s*\(JST\)", text)
    price = re.search(r"([\d,]+)円\(税込\)", text)
    if len(shows) != 4 or not period or not price or "https://l-tike.com/illit/" not in page.links:
        raise ValueError("ILLIT 日本场次、抽选窗口或价格字段缺失")
    year, month, day, opening, end_month, end_day, closing = period.groups()
    sale_start = datetime.fromisoformat("%04d-%02d-%02dT%s" % (int(year), int(month), int(day), opening))
    sale_end = datetime.fromisoformat("%04d-%02d-%02dT%s" % (int(year), int(end_month), int(end_day), closing))
    if sale_end <= sale_start:
        raise ValueError("ILLIT 日本抽选窗口顺序异常")
    result = []
    for y, m, d, doors, show in shows:
        show_date = date(int(y), int(m), int(d)).isoformat()
        if sale_end.date().isoformat() >= show_date:
            raise ValueError("抽选日期不能晚于演出日期")
        result.append({
            "title": "ILLIT LIVE 'PRESS START♥︎' ENCORE in JAPAN · 东京湾（千叶）",
            "tour_name": "ILLIT LIVE 'PRESS START♥︎' ENCORE",
            "city": "千叶（东京湾）", "country": "日本", "venue": "LaLa arena TOKYO-BAY",
            "show_date": show_date, "doors_time": doors.zfill(5), "show_time": show.zfill(5),
            "price": "JPY "+price.group(1)+"（含税）", "sale_status": "scheduled",
            "sale_time": sale_start.isoformat(timespec="minutes")+"+09:00",
            "sale_end_time": sale_end.isoformat(timespec="minutes")+"+09:00",
            "ticket_url": "https://l-tike.com/illit/",
            "note": "罗森抽选 %s 至 %s（日本时间），非先到先得；本轮截止后等待后续票务公告。开门与开演均为日本时间。" % (
                sale_start.strftime("%m/%d %H:%M"), sale_end.strftime("%m/%d %H:%M")),
        })
    if len({e["show_date"] for e in result}) != len(result):
        raise ValueError("日本公告存在重复场次，需要复核")
    return result


READERS = {
    "illit_nol_encore": ("https://nol.yanolja.com/ticket/products/", _korea),
    "illit_japan_encore": ("https://illit-official.jp/news/", _japan),
}


def collect(artist, source, cache_ttl=0):
    prefix, parse = READERS[source["parser"]]
    url = source["url"]
    if artist["key"] != "illit" or not url.startswith(prefix):
        raise ValueError("官方来源与采集器不匹配")
    html, error = http.get(url, cache_ttl=cache_ttl)
    if error or not html:
        raise ValueError("官方页面获取失败：%s" % (error or "空响应"))
    events = parse(Page(html))
    for event in events:
        event.update({
            "artist_key": artist["key"], "artist_name": artist["name"],
            "source": "official", "confidence": "confirmed", "url": url,
            "source_id": source["parser"]+":"+event["show_date"],
        })
    return events
