"""Watch upcoming events at chosen Bangkok venues; build a phone-friendly page
(docs/index.html, served by GitHub Pages) and optionally send Telegram alerts.

Staff/usher jobs are recruited by agencies 1-3 weeks before an event, so
knowing what is coming to BITEC, QSNCC, Rajamangala, etc. early is the
head start. Runs on GitHub Actions; uses only the Python standard library.

Env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  - optional; without them only the page is updated
  DRY_RUN=1                             - print messages, don't send, don't save state
"""

import html
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BKK = timezone(timedelta(hours=7))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
UA = {"User-Agent": "Mozilla/5.0 (event-staff-alert)", "Accept": "application/json,text/html"}
TH_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
FAIL_ALERT_AFTER = 3  # consecutive failed runs before warning that a source broke


def fetch(url, timeout=60, tries=2):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == tries - 1:
                raise


def km_between(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def match_zone(zones, text="", lat=None, lon=None):
    low = (text or "").lower()
    for z in zones:
        if any(k.lower() in low for k in z["keywords"]):
            return z["name"]
        if lat and lon and km_between(lat, lon, z["lat"], z["lon"]) <= z["radius_km"]:
            return z["name"]
    return None


# ---------- sources: each returns a list of event dicts ----------

def src_bitec(zones):
    """BITEC's own site API (JSON)."""
    out, page, per = [], 1, 12  # the site's own page size; bigger pages time out
    while page <= 15:
        url = ("https://www.bitec.co.th/api/content/events?mode=filtered&eventType=upcoming"
               f"&page={page}&perPage={per}&language=en")
        evs = json.loads(fetch(url)).get("events") or []
        for e in evs:
            f = e.get("eventFieldGroup") or {}
            start = datetime.strptime(f["eventStartdate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BKK)
            end = datetime.strptime(f.get("eventEnddate") or f["eventStartdate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BKK)
            cats = ", ".join(n["name"] for n in (e.get("eventCategories") or {}).get("nodes", []))
            out.append({
                "key": "bitec:" + str(e["id"]),
                "title": e["title"],
                "start": start, "end": end,
                "zone": "ไบเทค บางนา",
                "place": " · ".join(x for x in [f.get("eventHall"), cats] if x),
                "url": "https://www.bitec.co.th/whats-on/" + e["slug"],
            })
        if len(evs) < per:
            break
        page += 1
    return out


def src_qsncc(zones):
    """QSNCC event calendar; the next upcoming events are embedded in the page."""
    h = fetch("https://www.qsncc.com/en/whats-on/event-calendar/")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    if not m:
        raise RuntimeError("page layout changed (no __NEXT_DATA__)")
    results = json.loads(m.group(1))["props"]["pageProps"]["calendarEvents"]["results"]
    out = []
    for r in results:
        d = r["data"]
        if not d.get("startDate2"):
            continue
        start = datetime.strptime(d["startDate2"], "%Y-%m-%dT%H:%M:%S%z").astimezone(BKK)
        end = datetime.strptime(d.get("endDate2") or d["startDate2"], "%Y-%m-%dT%H:%M:%S%z").astimezone(BKK)
        out.append({
            "key": "qsncc:" + r["uid"],
            "title": d.get("title") or r["uid"],
            "start": start, "end": end,
            "zone": "ศูนย์ประชุมฯ สิริกิติ์",
            "place": d.get("location") or "",
            "url": "https://www.qsncc.com/en/whats-on/event-calendar/" + r["uid"],
        })
    return out


def src_theconcert(zones):
    """theconcert.com public concert list; kept only if the venue is in one of our zones."""
    out = []
    for page in range(1, 41):
        j = json.loads(fetch(f"https://apic.theconcert.com/v4/concert/lists?currency=THB&lang=th&page={page}"))
        recs = (j.get("data") or {}).get("record") or []
        if not recs:
            break
        for c in recs:
            v = c.get("venue") or {}
            zone = match_zone(zones, (v.get("name") or "") + " " + (v.get("address") or ""), v.get("lat"), v.get("long"))
            if not zone:
                continue
            st = c.get("show_time") or {}
            start = datetime.strptime(st["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BKK)
            end = datetime.strptime(st.get("end") or st["start"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BKK)
            out.append({
                "key": "theconcert:" + str(c["id"]),
                "title": c.get("name") or "",
                "start": start, "end": end,
                "zone": zone,
                "place": v.get("name") or "",
                "url": "https://www.theconcert.com/concert/" + str(c["id"]),
            })
        pag = (j.get("data") or {}).get("pagination") or {}
        last = pag.get("last_page") or pag.get("total_page") or pag.get("total_pages")
        if last and page >= int(last):
            break
    return out


SOURCES = {"BITEC": src_bitec, "QSNCC": src_qsncc, "theconcert.com": src_theconcert}


# ---------- formatting ----------

def th_date_range(s, e):
    y = s.year
    if s.date() == e.date():
        return f"{s.day} {TH_MONTHS[s.month]} {y}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}–{e.day} {TH_MONTHS[s.month]} {y}"
    return f"{s.day} {TH_MONTHS[s.month]} – {e.day} {TH_MONTHS[e.month]} {e.year}"


def fb_search(title):
    short = re.sub(r"\s+", " ", re.sub(r"[\(\)\[\]|:–-]", " ", title)).strip()[:40]
    return "https://www.facebook.com/search/posts/?q=" + urllib.parse.quote("staff " + short)


def fmt_event(ev, today):
    days = (ev["start"].date() - today).days
    when = "วันนี้" if days == 0 else ("กำลังจัดอยู่" if days < 0 else f"อีก {days} วัน")
    place = ev["zone"] + (f" · {ev['place']}" if ev["place"] else "")
    return (
        f"🎪 <b>{html.escape(ev['title'])}</b>\n"
        f"📍 {html.escape(place)}\n"
        f"📅 {th_date_range(ev['start'], ev['end'])} ({when})\n"
        f"🔗 <a href=\"{html.escape(ev['url'])}\">รายละเอียดงาน</a> · "
        f"<a href=\"{html.escape(fb_search(ev['title']))}\">หาประกาศสตาฟใน FB</a>"
    )


def chunk_messages(header, blocks, limit=3800):
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = header + " (ต่อ)"
        cur += "\n\n" + b
    msgs.append(cur)
    return msgs


# ---------- delivery & state ----------

def send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("DRY_RUN") or not token or not chat:
        print("----- message -----\n" + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
        r.read()


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "first_seen": {}, "reminded": [], "fails": {}}


def write_page(upcoming, state, cfg, now, source_counts):
    """Write docs/index.html, the phone-friendly list served by GitHub Pages."""
    new_since = (now.date() - timedelta(days=3)).isoformat()
    data = [{
        "title": e["title"], "zone": e["zone"], "place": e["place"],
        "start": e["start"].date().isoformat(), "end": e["end"].date().isoformat(),
        "url": e["url"], "fb": fb_search(e["title"]),
        "isNew": state["first_seen"].get(e["key"], "") >= new_since,
    } for e in upcoming]
    with open(os.path.join(HERE, "page_template.html"), encoding="utf-8") as f:
        page = f.read()
    sources = " · ".join(f"{n} {'✓' if c is not None else '✗'}" for n, c in source_counts.items())
    updated = f"{now.day} {TH_MONTHS[now.month]} {now.year} {now:%H:%M} น."
    # json.dumps output is safe inside <script> once "</" is escaped.
    page = (page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__AVOID__", json.dumps(cfg.get("avoid_dates", [])))
                .replace("__REMIND__", str(int(cfg["remind_days_before"])))
                .replace("__UPDATED__", html.escape(updated))
                .replace("__SOURCES__", html.escape(sources)))
    os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
    with open(os.path.join(HERE, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    state = load_state()
    state.setdefault("first_seen", {})
    now = datetime.now(BKK)
    today = now.date()
    horizon = today + timedelta(days=cfg["look_ahead_days"])

    events, problems, counts = [], [], {}
    for name, fn in SOURCES.items():
        try:
            got = fn(cfg["zones"])
            print(f"{name}: {len(got)} events")
            events += got
            counts[name] = len(got)
            state["fails"][name] = 0
        except Exception as e:  # one broken site must not stop the others
            counts[name] = None
            n = state["fails"].get(name, 0) + 1
            state["fails"][name] = n
            print(f"{name}: FAILED ({n} in a row): {e}", file=sys.stderr)
            if n == FAIL_ALERT_AFTER:
                problems.append(f"⚠️ ดึงข้อมูลจาก {name} ไม่ได้ {n} รอบติดกัน เว็บอาจเปลี่ยนรูปแบบ ({html.escape(str(e))[:120]})")

    upcoming = sorted(
        (e for e in events if e["end"].date() >= today and e["start"].date() <= horizon),
        key=lambda e: e["start"],
    )
    new, remind = [], []
    for ev in upcoming:
        days = (ev["start"].date() - today).days
        if ev["key"] not in state["seen"]:
            new.append(ev)
            state["seen"][ev["key"]] = ev["end"].date().isoformat()
            state["first_seen"][ev["key"]] = today.isoformat()
            if days <= cfg["remind_days_before"]:
                state["reminded"].append(ev["key"])
        elif 0 <= days <= cfg["remind_days_before"] and ev["key"] not in state["reminded"]:
            remind.append(ev)
            state["reminded"].append(ev["key"])

    messages = []
    if new:
        messages += chunk_messages(f"🆕 <b>งานใหม่ในโซนของคุณ {len(new)} งาน</b>", [fmt_event(e, today) for e in new])
    if remind:
        messages += chunk_messages(
            f"⏰ <b>อีกไม่เกิน {cfg['remind_days_before']} วันถึงงาน เอเจนซี่น่าจะกำลังหาสตาฟ</b>",
            [fmt_event(e, today) for e in remind],
        )
    messages += problems

    # Forget events that ended over a week ago so state.json stays small.
    cutoff = (today - timedelta(days=7)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["reminded"] = sorted(k for k in set(state["reminded"]) if k in state["seen"])
    state["first_seen"] = {k: v for k, v in state["first_seen"].items() if k in state["seen"]}

    write_page(upcoming, state, cfg, now, counts)
    if os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("DRY_RUN"):
        for m in messages:
            send(m)
    print(f"new={len(new)} remind={len(remind)} problems={len(problems)}")
    if not os.environ.get("DRY_RUN"):
        save_state(state)


if __name__ == "__main__":
    main()
