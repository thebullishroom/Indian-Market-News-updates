#!/usr/bin/env python3
"""
Indian market news bot for Discord (NSE + BSE).

Modes:
  morning  8:30 AM IST trading days: today's results, board meetings, ex-dates,
           economic events, then catches up on overnight filings
  alerts   every 15 min in market hours + evening: new results, dividend,
           bonus and split announcements (never the same alert twice)
  weekly   Sunday 6 PM IST: next week's calendar, grouped by day

Usage:
  python india_news.py morning|alerts|weekly [--date YYYY-MM-DD] [--dry-run] [--force]
  python india_news.py alerts --fixtures tests/news_fixtures   # offline testing

Settings (environment variables):
  DISCORD_WEBHOOK_INDIA_NEWS  Discord webhook URL (required unless --dry-run)
  NEWS_UNIVERSE               "fno" (default: F&O stocks only) or "all"
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("INDIA_NEWS_STATE_FILE", ROOT / "state" / "india_news_state.json"))
CAL_FILE = ROOT / "data" / "india_economic_calendar.json"

UNIVERSE = os.environ.get("NEWS_UNIVERSE", "fno").strip().lower() or "fno"
MAX_ALERTS_PER_RUN = 40      # anything beyond this is summarised in one line
FIRST_RUN_LOOKBACK_MIN = 60  # very first run: only alert on the last hour
SEEN_KEEP_DAYS = 6
MAX_LIST = 40                # names per list in briefs before "+N more"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
NSE_HOME = "https://www.nseindia.com"
NSE_ARCH = "https://nsearchives.nseindia.com"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# NSE trading holidays 2026 (weekdays), per NSE circular of 12 Dec 2025 plus the
# 15 Jan Maharashtra civic election closure. Used only if NSE's holiday API fails.
HOLIDAYS_FALLBACK = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day", "2026-03-03": "Holi", "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti", "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id", "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti", "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada", "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}


class SourceError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class Sources:
    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures
        self.nse_s = requests.Session()
        self.nse_s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                   "Referer": NSE_HOME + "/"})
        self.bse_s = requests.Session()
        self.bse_s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                                   "Referer": "https://www.bseindia.com/",
                                   "Origin": "https://www.bseindia.com"})
        self._warm = False

    def _fixture(self, key: str):
        p = self.fixtures / key
        if not p.exists():
            raise SourceError(f"fixture {key} missing")
        return json.loads(p.read_text()) if key.endswith(".json") else p.read_bytes()

    def _warmup(self):
        if not self._warm:
            for url in (NSE_HOME, NSE_HOME + "/companies-listing/corporate-filings-announcements"):
                try:
                    self.nse_s.get(url, timeout=15)
                except requests.RequestException:
                    pass
            self._warm = True

    def _get_json(self, sess, url, params, nse: bool):
        last = ""
        for attempt in range(3):
            try:
                r = sess.get(url, params=params, timeout=25)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                last = type(e).__name__
            if nse:
                self._warm = False
                self._warmup()
            time.sleep(2 * (attempt + 1))
        raise SourceError(f"{url} -> {last}")

    def nse(self, path: str, params: dict, key: str):
        if self.fixtures:
            return self._fixture(key)
        self._warmup()
        return self._get_json(self.nse_s, NSE_HOME + path, params, nse=True)

    def bse(self, path: str, params: dict, key: str):
        if self.fixtures:
            return self._fixture(key)
        return self._get_json(self.bse_s, f"{BSE_API}/{path}", params, nse=False)

    def archive(self, path: str, key: str) -> bytes:
        if self.fixtures:
            return self._fixture(key)
        last = ""
        for attempt in range(3):
            try:
                r = self.nse_s.get(NSE_ARCH + path, timeout=30)
                if r.status_code == 200 and not r.content.lstrip()[:15].lower().startswith(b"<!doctype"):
                    return r.content
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
            time.sleep(2 * (attempt + 1))
        raise SourceError(f"{path} -> {last}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
DT_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
              "%Y-%m-%dT%H:%M:%S", "%d %b %Y %H:%M:%S", "%d-%b-%Y", "%d %b %Y",
              "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%Y%m%d")


def parse_dt(s) -> datetime | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ("-", "--"):
        return None
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)  # 2026-11-22T18:30:01.887
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S") if m else None


def parse_d(s) -> date | None:
    dt = parse_dt(s)
    return dt.date() if dt else None


def g(row: dict, *keys, default=""):
    """First non-empty value among several possible field names."""
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return str(v).strip()
    return default


STOP = {"limited", "ltd", "the", "company", "co", "corporation", "corp", "inc", "plc"}


def norm_name(n: str) -> str:
    n = (n or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return " ".join(w for w in n.split() if w not in STOP)


def rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("data", "Table", "rows"):
            if isinstance(data.get(k), list):
                return [r for r in data[k] if isinstance(r, dict)]
    return []


def nse_d(d: date) -> str:
    return f"{d:%d-%m-%Y}"


def bse_d(d: date) -> str:
    return f"{d:%Y%m%d}"


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- #
# Holidays, universe, economic calendar
# --------------------------------------------------------------------------- #
def holidays(src: Sources, st: dict, year: int, problems: list) -> dict[str, str]:
    cache = st.setdefault("holidays", {})
    entry = cache.get(str(year))
    if entry and entry.get("fetched", "") >= (datetime.now(IST).date() - timedelta(days=7)).isoformat():
        return entry["days"]
    if getattr(src, "holiday_api_failed", False):
        return entry["days"] if entry else {k: v for k, v in HOLIDAYS_FALLBACK.items() if k.startswith(str(year))}
    try:
        data = src.nse("/api/holiday-master", {"type": "trading"}, "nse_holidays.json")
        days = {}
        for r in (data.get("CM") or data.get("FO") or []) if isinstance(data, dict) else []:
            d = parse_d(g(r, "tradingDate", "date"))
            if d and d.year == year:
                days[d.isoformat()] = g(r, "description", default="Market holiday")
        if days:
            cache[str(year)] = {"fetched": datetime.now(IST).date().isoformat(), "days": days}
            return days
    except SourceError as e:
        src.holiday_api_failed = True
        problems.append(f"NSE holidays: {e} (using built-in 2026 list)")
    if entry:
        return entry["days"]
    return {k: v for k, v in HOLIDAYS_FALLBACK.items() if k.startswith(str(year))}


def holiday_name(src, st, d: date, problems) -> str | None:
    if d.weekday() >= 5:
        return "Weekend"
    return holidays(src, st, d.year, problems).get(d.isoformat())


class Universe:
    """F&O stock list plus NSE company names, for filtering and BSE→NSE matching."""

    def __init__(self, fno: set[str] | None, names: dict[str, str]):
        self.fno = fno
        self.names = names
        self.by_norm = {norm_name(n): s for s, n in names.items()}

    def match(self, symbol: str = "", name: str = "") -> str:
        s = (symbol or "").upper()
        if s in self.names or (self.fno and s in self.fno):
            return s
        return self.by_norm.get(norm_name(name), "")

    def wanted(self, symbol: str) -> bool:
        if UNIVERSE == "all" or self.fno is None:
            return True
        return symbol in self.fno


def load_universe(src: Sources, st: dict, today: date, problems: list) -> Universe:
    cache = st.get("universe", {})
    fresh = cache.get("fetched", "") >= (today - timedelta(days=7)).isoformat()
    if not fresh:
        fno, names = None, {}
        try:
            raw = src.archive("/content/fo/fo_mktlots.csv", "fo_mktlots.csv").decode("utf-8", "replace")
            rdr = list(csv.reader(io.StringIO(raw)))
            hdr = [h.strip().upper() for h in rdr[0]]
            i = hdr.index("SYMBOL")
            fno = {r[i].strip().upper() for r in rdr[1:] if len(r) > i and r[i].strip()
                   and r[i].strip().upper() != "SYMBOL"}
        except (SourceError, ValueError, IndexError) as e:
            problems.append(f"F&O list: {e}")
        try:
            raw = src.archive("/content/equities/EQUITY_L.csv", "EQUITY_L.csv").decode("utf-8", "replace")
            for r in csv.DictReader(io.StringIO(raw)):
                r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
                if r.get("SYMBOL"):
                    names[r["SYMBOL"].upper()] = r.get("NAME OF COMPANY", "")
        except SourceError as e:
            problems.append(f"NSE company list: {e}")
        if fno or names:
            cache = {"fetched": today.isoformat(),
                     "fno": sorted(fno) if fno else cache.get("fno"),
                     "names": names or cache.get("names", {})}
            st["universe"] = cache
    fno = set(cache["fno"]) if cache.get("fno") else None
    if fno is None and UNIVERSE != "all":
        problems.append("F&O list unavailable, so this run covers all companies")
    return Universe(fno, cache.get("names", {}))


def econ_events(a: date, b: date) -> dict[date, list[str]]:
    out: dict[date, list[str]] = {}
    try:
        events = json.loads(CAL_FILE.read_text()).get("events", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return out
    for e in events:
        d = parse_d(e.get("date"))
        if d and a <= d <= b:
            t = f" · {e['time']}" if e.get("time") else ""
            out.setdefault(d, []).append(f"{e['event']}{t}")
    return out


# --------------------------------------------------------------------------- #
# Calendar: board meetings and ex-dates
# --------------------------------------------------------------------------- #
RESULTS_RE = re.compile(r"financial results?|quarterly results?|\bresults?\b", re.I)
NOTABLE_RE = re.compile(r"fund ?rais|\bqip\b|preferential|rights issue|dividend|\bbonus\b|split|"
                        r"sub-?division|buy ?back|debenture|\bncds?\b", re.I)
ACTION_KINDS = (("Dividend", re.compile(r"dividend", re.I)),
                ("Bonus", re.compile(r"\bbonus\b", re.I)),
                ("Split", re.compile(r"split|sub-?division", re.I)))


def calendar(src: Sources, uni: Universe, a: date, b: date, problems: list) -> dict:
    meetings: dict[tuple, dict] = {}
    exdates: dict[tuple, dict] = {}

    def add_meeting(d, symbol, name, purpose, detail=""):
        if not d or not (a <= d <= b):
            return
        both = f"{purpose} {detail}"
        kind = "results" if RESULTS_RE.search(purpose) or (not purpose and RESULTS_RE.search(detail)) \
            else "other" if NOTABLE_RE.search(both) else None
        if not kind:
            return
        key = (symbol or norm_name(name), d, kind)
        meetings.setdefault(key, {"date": d, "symbol": symbol, "name": name,
                                  "purpose": clip(purpose, 90), "kind": kind})

    def add_action(d, symbol, name, subject):
        if not d or not (a <= d <= b):
            return
        for kind, rx in ACTION_KINDS:
            if rx.search(subject):
                key = (symbol or norm_name(name), d, kind)
                exdates.setdefault(key, {"date": d, "symbol": symbol, "name": name,
                                         "kind": kind, "detail": clip(subject, 80)})
                break

    ok = {"NSE": False, "BSE": False}
    try:
        for r in rows(src.nse("/api/corporate-board-meetings",
                              {"index": "equities", "from_date": nse_d(a), "to_date": nse_d(b)},
                              "nse_board_meetings.json")):
            sym = g(r, "bm_symbol", "symbol").upper()
            add_meeting(parse_d(g(r, "bm_date", "meetingDate", "date")), sym,
                        g(r, "sm_name", "companyName", "comp"),
                        g(r, "bm_purpose", "purpose"), g(r, "bm_desc"))
        ok["NSE"] = True
    except SourceError as e:
        problems.append(f"NSE board meetings: {e}")
    try:
        for r in rows(src.nse("/api/corporates-corporateActions",
                              {"index": "equities", "from_date": nse_d(a), "to_date": nse_d(b)},
                              "nse_corp_actions.json")):
            add_action(parse_d(g(r, "exDate", "ex_date")), g(r, "symbol").upper(),
                       g(r, "comp", "companyName"), g(r, "subject", "purpose"))
        ok["NSE"] = True
    except SourceError as e:
        problems.append(f"NSE corporate actions: {e}")
    try:
        for r in rows(src.bse("Corpforthresults/w",
                              {"fromdate": bse_d(a), "scripcode": "", "todate": bse_d(b)},
                              "bse_results_calendar.json")):
            name = g(r, "Long_Name", "long_name", "LONG_NAME", "short_name")
            sym = uni.match(g(r, "short_name", "ShortName"), name)
            add_meeting(parse_d(g(r, "meeting_date", "MeetingDate", "Meeting_Date")),
                        sym, name, "Financial Results")
        ok["BSE"] = True
    except SourceError as e:
        problems.append(f"BSE results calendar: {e}")
    try:
        for r in rows(src.bse("DefaultData/w",
                              {"Fdate": bse_d(a), "Purposecode": "", "TDate": bse_d(b),
                               "ddlcategorys": "E", "ddlindustrys": "", "scripcode": "",
                               "segment": "0", "strSearch": "S"},
                              "bse_corp_actions.json")):
            name = g(r, "long_name", "LONG_NAME", "short_name")
            sym = uni.match(g(r, "short_name"), name)
            add_action(parse_d(g(r, "Ex_date", "exdate", "EX_DATE")), sym, name,
                       g(r, "Purpose", "purpose"))
        ok["BSE"] = True
    except SourceError as e:
        problems.append(f"BSE corporate actions: {e}")

    # A company can appear from both exchanges under symbol and name keys: keep one.
    def dedupe(items: dict) -> list[dict]:
        out, seen = [], set()
        for it in sorted(items.values(), key=lambda x: (x["date"], 0 if x["symbol"] else 1)):
            k = (norm_name(uni.names.get(it["symbol"], "") or it["name"]) or it["symbol"],
                 it["date"], it["kind"])
            k2 = (it["symbol"], it["date"], it["kind"])
            if k in seen or (it["symbol"] and k2 in seen):
                continue
            seen.update({k, k2})
            out.append(it)
        return out

    return {"meetings": dedupe(meetings), "exdates": dedupe(exdates), "ok": ok}


def label(it: dict) -> str:
    return it["symbol"] or clip(it["name"], 30) or "?"


def split_universe(items: list[dict], uni: Universe) -> tuple[list[dict], int]:
    keep = [i for i in items if i["symbol"] and uni.wanted(i["symbol"])] \
        if UNIVERSE != "all" and uni.fno else items
    return keep, len(items) - len(keep)


def others_text(n: int) -> str:
    if not n:
        return ""
    word = "company" if n == 1 else "companies"
    return f"{n} non-F&O {word}" if UNIVERSE != "all" else f"{n} more {word}"


def with_others(shown: list[str], hidden: int, others: int) -> str:
    text = ", ".join(shown)
    if hidden:
        text += f" + {hidden} more"
    if others:
        text += f" (+{others_text(others)})" if text else others_text(others)
    return text or "None"


def join_names(items: list[dict], others: int, limit: int = MAX_LIST) -> str:
    names = sorted({label(i) for i in items})
    return with_others(names[:limit], max(0, len(names) - limit), others)


def action_text(i: dict) -> str:
    d = i["detail"]
    m = re.search(r"(?:rs\.?|₹|inr)\s*([\d.]+)", d, re.I)
    if i["kind"] == "Dividend" and m:
        return f"{label(i)} div ₹{m.group(1).rstrip('.')}"
    m = re.search(r"(\d+)\s*:\s*(\d+)", d)
    if i["kind"] == "Bonus" and m:
        return f"{label(i)} bonus {m.group(1)}:{m.group(2)}"
    m = re.search(r"rs\.?\s*([\d.]+).{0,25}?rs\.?\s*([\d.]+)", d, re.I)
    if i["kind"] == "Split" and m:
        return f"{label(i)} split ₹{m.group(1).rstrip('.')}→₹{m.group(2).rstrip('.')}"
    return f"{label(i)} {i['kind'].lower()}"


# --------------------------------------------------------------------------- #
# Live announcements
# --------------------------------------------------------------------------- #
INTIMATION_RE = re.compile(r"intimation|\bto consider\b|\bwill consider\b|\bproposal to consider\b|"
                           r"\bis scheduled to be held\b|\bshall be held\b|\bwill be held\b", re.I)
RESULT_TEXT_RE = re.compile(r"(audited|unaudited|un-audited|financial)\s+(standalone\s+|consolidated\s+)?"
                            r"(and\s+consolidated\s+)?(financial\s+)?results?", re.I)
DECISION_RE = re.compile(r"declar|recommend|approv|record date|interim|final dividend|"
                         r"ex-?date|allot", re.I)
TAG_ICONS = {"Results": "📊", "Dividend": "💸", "Bonus": "🎁", "Split": "✂️"}
TAG_COLORS = {"Results": 0x3498DB, "Dividend": 0x2ECC71, "Bonus": 0xF1C40F, "Split": 0x9B59B6}


def classify(desc: str, text: str, category: str = "") -> list[str]:
    d, t, cat = desc.lower(), f"{desc} {text}".lower(), category.lower()
    if INTIMATION_RE.search(d) or "board meeting intimation" in cat:
        return []
    upcoming = bool(INTIMATION_RE.search(t))
    tags = []
    if cat == "result" or re.search(r"financial result|integrated filing.{0,3}financial", d) or (
            re.search(r"outcome|board meeting", d + " " + cat) and RESULT_TEXT_RE.search(t)
            and not upcoming):
        tags.append("Results")
    if not upcoming:
        if "dividend" in t and "dividend distribution policy" not in t and (
                "dividend" in d or DECISION_RE.search(t)):
            tags.append("Dividend")
        if re.search(r"\bbonus\b", t) and ("bonus" in d or DECISION_RE.search(t)):
            tags.append("Bonus")
        if re.search(r"stock split|sub-?division|split of (the )?(equity )?shares|face value split", t) \
                and (re.search(r"split|sub-?division", d) or DECISION_RE.search(t)):
            tags.append("Split")
    return tags


def announcements(src: Sources, uni: Universe, a: date, b: date, problems: list) -> tuple[list, dict]:
    items = []
    ok = {"NSE": False, "BSE": False}
    try:
        for r in rows(src.nse("/api/corporate-announcements",
                              {"index": "equities", "from_date": nse_d(a), "to_date": nse_d(b)},
                              "nse_announcements.json")):
            when = parse_dt(g(r, "sort_date", "an_dt", "dt"))
            desc, text = g(r, "desc"), g(r, "attchmntText")
            sym = g(r, "symbol").upper()
            items.append({"id": "nse:" + (g(r, "seq_id") or f"{sym}|{when}|{desc}"),
                          "source": "NSE", "symbol": sym, "name": g(r, "sm_name"),
                          "when": when, "tags": classify(desc, text), "title": desc,
                          "text": text, "url": g(r, "attchmntFile")})
        ok["NSE"] = True
    except SourceError as e:
        problems.append(f"NSE announcements: {e}")
    try:
        for page in range(1, 21):
            data = src.bse("AnnSubCategoryGetData/w",
                           {"pageno": page, "strCat": "-1", "strPrevDate": bse_d(a),
                            "strScrip": "", "strSearch": "P", "strToDate": bse_d(b),
                            "strType": "C", "subcategory": "-1"},
                           f"bse_announcements_p{page}.json")
            page_rows = rows(data)
            for r in page_rows:
                name = g(r, "SLONGNAME", "SHORTNAME")
                sym = uni.match("", name)
                headline = g(r, "HEADLINE")
                sub = g(r, "NEWSSUB")
                cat = g(r, "CATEGORYNAME")
                desc = g(r, "SUBCATNAME") or cat
                att = g(r, "ATTACHMENTNAME")
                items.append({"id": "bse:" + (g(r, "NEWSID") or f"{name}|{sub}"),
                              "source": "BSE", "symbol": sym, "name": name,
                              "when": parse_dt(g(r, "DT_TM", "NEWS_DT", "DissemDT")),
                              "tags": classify(desc + " " + sub, headline, cat),
                              "title": clip(re.sub(r"^.*? - \d{6} - ", "", sub) or desc, 120),
                              "text": headline, "url": BSE_ATTACH + att if att else ""})
            total_pages = 0
            if isinstance(data, dict) and data.get("Table1"):
                total_pages = int(num_or(data["Table1"][0].get("ROWCNT"), 0) // 50 + 1)
            if not page_rows or (total_pages and page >= total_pages):
                break
        ok["BSE"] = True
    except SourceError as e:
        if not ok["BSE"] and not any(i["source"] == "BSE" for i in items):
            problems.append(f"BSE announcements: {e}")
        else:
            ok["BSE"] = True  # later page missing; earlier pages fine
    return items, ok


def num_or(v, default):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def pick_new_alerts(items: list[dict], uni: Universe, st: dict, now: datetime) -> list[dict]:
    seen = st.setdefault("seen", {})
    sent = st.setdefault("sent_events", {})
    first_run = not st.get("seeded")
    today = now.date().isoformat()
    out = []
    for it in sorted(items, key=lambda x: x["when"] or datetime.min):
        if it["id"] in seen:
            continue
        seen[it["id"]] = today
        if not it["tags"]:
            continue
        if first_run and (it["when"] is None or it["when"] < now.replace(tzinfo=None)
                          - timedelta(minutes=FIRST_RUN_LOOKBACK_MIN)):
            continue
        sym = it["symbol"]
        if UNIVERSE != "all" and uni.fno and not (sym and uni.wanted(sym)):
            continue
        company = sym or norm_name(it["name"])
        day = (it["when"].date() if it["when"] else now.date()).isoformat()
        new_tags = [t for t in it["tags"] if f"{company}|{t}|{day}" not in sent]
        if not new_tags:
            continue  # same news already alerted from the other exchange
        for t in new_tags:
            sent[f"{company}|{t}|{day}"] = today
        out.append(dict(it, tags=new_tags))
    st["seeded"] = True
    cutoff = (now.date() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    for dct in (seen, sent):
        for k in [k for k, v in dct.items() if v < cutoff]:
            del dct[k]
    return out


def alert_embed(it: dict, uni: Universe) -> dict:
    tags = it["tags"]
    who = it["symbol"] or clip(it["name"], 40)
    full = uni.names.get(it["symbol"]) or it["name"]
    title = clip(f"{''.join(TAG_ICONS[t] for t in tags)} {' + '.join(tags)} · {who}", 250)
    desc = []
    if full and full.upper() != who.upper():
        desc.append(f"**{clip(full, 80)}**")
    if it["title"]:
        desc.append(clip(it["title"], 150))
    if it["text"] and it["text"] != it["title"]:
        desc.append(clip(it["text"], 350))
    if it["url"]:
        desc.append(f"[Open filing]({it['url']})")
    when = f" · {it['when']:%d %b, %I:%M %p} IST" if it["when"] else ""
    e = {"title": title, "description": "\n".join(desc) or "New filing",
         "color": TAG_COLORS[tags[0]], "footer": {"text": f"{it['source']}{when}"}}
    if it["url"]:
        e["url"] = it["url"]
    return e


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def batches(embeds: list[dict], max_chars: int = 5500) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for e in embeds:
        n = len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
        if cur and (size + n > max_chars or len(cur) == 10):
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        out.append(cur)
    return out


class Poster:
    def __init__(self, dry_run: bool):
        self.dry = dry_run
        self.hook = os.environ.get("DISCORD_WEBHOOK_INDIA_NEWS", "").strip()
        if not dry_run and not self.hook:
            sys.exit("DISCORD_WEBHOOK_INDIA_NEWS secret is not set")

    def send(self, content: str, embeds: list[dict]) -> None:
        for e in embeds:
            e["description"] = clip_block(e.get("description", ""), 4000)
        if self.dry:
            if content:
                print("\n" + content)
            for e in embeds:
                print(f"\n## {e['title']}\n{e['description']}")
                if e.get("footer"):
                    print(f"-- {e['footer']['text']}")
            return
        groups = batches(embeds) or [[]]
        for i, grp in enumerate(groups):
            payload = {"embeds": grp, "allowed_mentions": {"parse": []}}
            if i == 0 and content:
                payload["content"] = content
            for _ in range(4):
                r = requests.post(self.hook, json=payload, timeout=20)
                if r.status_code == 429:
                    time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                    continue
                r.raise_for_status()
                break
            time.sleep(1)


def clip_block(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[: n - 20].rsplit("\n", 1)[0]
    return cut + "\n…and more"


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def day_embed(title: str, d: date, cal: dict, econ: dict, uni: Universe, hol: str | None,
              color: int, empty_text: str) -> dict:
    lines = []
    if hol and hol != "Weekend":
        lines.append(f"🚫 **Market holiday:** {hol}")
    res, res_other = split_universe([m for m in cal["meetings"] if m["date"] == d and m["kind"] == "results"], uni)
    if res or res_other:
        lines.append(f"📊 **Results ({len(res) + res_other}):** {join_names(res, res_other)}")
    oth, oth_other = split_universe([m for m in cal["meetings"] if m["date"] == d and m["kind"] == "other"], uni)
    if oth or oth_other:
        shown = [f"{label(m)} ({clip(m['purpose'], 45)})" for m in sorted(oth, key=label)[:15]]
        lines.append("🏛️ **Board meetings:** " + with_others(shown, len(oth) - len(shown), oth_other))
    ex, ex_other = split_universe([x for x in cal["exdates"] if x["date"] == d], uni)
    if ex or ex_other:
        shown = [action_text(x) for x in sorted(ex, key=label)[:25]]
        lines.append("✂️ **Ex-date:** " + with_others(shown, len(ex) - len(shown), ex_other))
    for ev in econ.get(d, []):
        lines.append(f"🏦 **{ev}**")
    return {"title": title, "description": "\n".join(lines) or empty_text, "color": color}


def run_morning(src, st, today, poster, force) -> int:
    problems: list[str] = []
    hol = holiday_name(src, st, today, problems)
    if hol:
        print(f"{today}: {hol}, no morning brief.")
        return 0
    if st.get("morning_posted") == today.isoformat() and not force:
        print("Morning brief already posted today.")
    else:
        uni = load_universe(src, st, today, problems)
        cal = calendar(src, uni, today, today, problems)
        econ = econ_events(today, today)
        if not any(cal["ok"].values()) and not econ.get(today):
            for p in problems:
                print("ERROR:", p)
            print("NSE and BSE both failed; no brief posted. The next run will retry.")
            return 1
        e = day_embed("📅 Today's calendar", today, cal, econ, uni, None, 0xF39C12,
                      "No results, ex-dates or major economic events today.")
        sections = []
        res = [m for m in cal["meetings"] if m["kind"] == "results"]
        failed = [k for k, v in cal["ok"].items() if not v]
        foot = "NSE + BSE" if not failed else f"{'/'.join(k for k in cal['ok'] if cal['ok'][k])} only " \
                                               f"({'/'.join(failed)} unavailable)"
        e["footer"] = {"text": f"Source: {foot} · Not investment advice"}
        sections.append(e)
        poster.send(f"☀️ **Morning Brief — {today:%a %d %b %Y}**"
                    + (f"  ·  {len(res)} companies report results today" if res else ""), sections)
        st["morning_posted"] = today.isoformat()
    rc = run_alerts(src, st, today, poster, problems=problems)
    return rc


def run_alerts(src, st, today, poster, problems=None) -> int:
    problems = problems if problems is not None else []
    hol = holiday_name(src, st, today, problems)
    if hol:
        print(f"{today}: {hol}, no live alerts.")
        return 0
    uni = load_universe(src, st, today, problems)
    last = parse_d(st.get("last_alert_day")) or today
    start = max(min(last, today), today - timedelta(days=4))
    items, ok = announcements(src, uni, start, today, problems)
    for p in problems:
        print("WARNING:", p)
    if not any(ok.values()):
        print("NSE and BSE both failed; nothing checked. The next run will retry.")
        return 1
    now = datetime.now(IST)
    if os.environ.get("NEWS_NOW"):  # testing only
        now = datetime.fromisoformat(os.environ["NEWS_NOW"]).replace(tzinfo=IST)
    alerts = pick_new_alerts(items, uni, st, now)
    st["last_alert_day"] = today.isoformat()
    print(f"Checked {len(items)} filings from {'/'.join(k for k, v in ok.items() if v)}; "
          f"{len(alerts)} new alert(s).")
    if not alerts:
        return 0
    shown = alerts[:MAX_ALERTS_PER_RUN]
    embeds = [alert_embed(a, uni) for a in shown]
    extra = len(alerts) - len(shown)
    content = ""
    if extra:
        content = f"⚡ {len(alerts)} new filings — showing the first {len(shown)}, {extra} more on NSE/BSE."
    poster.send(content, embeds)
    return 0


def run_weekly(src, st, today, poster, force) -> int:
    problems: list[str] = []
    start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)  # next Monday
    end = start + timedelta(days=6)
    if st.get("weekly_posted") == start.isoformat() and not force:
        print("Weekly preview already posted.")
        return 0
    uni = load_universe(src, st, today, problems)
    cal = calendar(src, uni, start, end, problems)
    econ = econ_events(start, end)
    if not any(cal["ok"].values()):
        for p in problems:
            print("ERROR:", p)
        print("NSE and BSE both failed; weekly preview not posted.")
        return 1
    embeds = []
    for i in range(7):
        d = start + timedelta(days=i)
        hol = holiday_name(src, st, d, problems)
        has = any(m["date"] == d for m in cal["meetings"]) or any(x["date"] == d for x in cal["exdates"]) \
            or econ.get(d) or (hol and hol != "Weekend")
        if d.weekday() >= 5 and not has:
            continue
        embeds.append(day_embed(f"{d:%A, %d %b}", d, cal, econ, uni, hol,
                                0x95A5A6 if hol else 0x5865F2, "Nothing major scheduled."))
    n_res = sum(1 for m in cal["meetings"] if m["kind"] == "results")
    failed = [k for k, v in cal["ok"].items() if not v]
    note = f" ({'/'.join(failed)} unavailable)" if failed else ""
    embeds[-1]["footer"] = {"text": f"Source: NSE, BSE, RBI, MoSPI{note} · Dates can change"}
    head = f"🗓️ **Week Ahead — {start:%d %b} to {end:%d %b %Y}**"
    if n_res:
        head += f"  ·  {n_res} companies report results"
    poster.send(head, embeds)
    st["weekly_posted"] = start.isoformat()
    for p in problems:
        print("WARNING:", p)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["morning", "alerts", "weekly"])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="post the brief/preview again")
    ap.add_argument("--fixtures", type=Path)
    a = ap.parse_args()
    today = date.fromisoformat(a.date) if a.date else datetime.now(IST).date()
    src = Sources(a.fixtures)
    st = load_state()
    poster = Poster(a.dry_run)
    if a.mode == "morning":
        rc = run_morning(src, st, today, poster, a.force)
    elif a.mode == "alerts":
        rc = run_alerts(src, st, today, poster)
    else:
        rc = run_weekly(src, st, today, poster, a.force)
    if not a.dry_run or a.fixtures:  # a dry run on GitHub mustn't mark filings as seen
        save_state(st)
    sys.exit(rc)


if __name__ == "__main__":
    main()
