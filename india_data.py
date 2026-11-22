#!/usr/bin/env python3
"""
Indian market evening data post for Discord.

Posts once per trading day (around 7-9 PM IST) using free NSE end-of-day files:
  * FII/DII cash market buy / sell / net, plus a month-to-date total
  * FII index futures long-short ratio and participant-wise OI (FII, DII, Pro, Client)
  * NIFTY and BANKNIFTY options: top call/put OI, biggest OI additions, PCR, max pain
  * Stock futures OI buildup: long buildup, short buildup, short covering, long unwinding
  * Delivery %: F&O stocks with unusually high delivery
  * Advance-decline ratio

Usage:
  python india_data.py                 # today's date (IST), post to Discord
  python india_data.py --date 2026-11-20
  python india_data.py --dry-run       # print instead of posting
  python india_data.py --final         # post with whatever data is available
  python india_data.py --fixtures DIR  # read files from DIR instead of NSE (testing)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("INDIA_STATE_FILE", ROOT / "state" / "india_data_state.json"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ARCHIVES = "https://nsearchives.nseindia.com"
HOME = "https://www.nseindia.com"

INDICES = ["NIFTY", "BANKNIFTY"]
TOP_N = 5                 # rows per list
MIN_TURNOVER_CR = 25      # delivery list: ignore illiquid stocks (₹ crore)
DELIVERY_SPIKE_X = 2.0    # delivery qty at least 2x its recent average
DELIVERY_MIN_PCT = 45.0   # ...and delivery % at least this
HISTORY_DAYS = 10         # days of delivery history kept per stock


class Blocked(Exception):
    """NSE refused us (403, timeouts, HTML instead of data)."""


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class NSE:
    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": HOME + "/",
        })
        self._warm = False

    def _warmup(self) -> None:
        """NSE's API wants the cookies set by the homepage first."""
        if self._warm:
            return
        for url in (HOME, HOME + "/reports/fii-dii"):
            try:
                self.s.get(url, timeout=15)
            except requests.RequestException:
                pass
        self._warm = True

    def file(self, url: str) -> bytes | None:
        """Download an archive file. None = not published (404). Raises Blocked."""
        if self.fixtures:
            p = self.fixtures / url.rsplit("/", 1)[-1]
            return p.read_bytes() if p.exists() else None
        last = ""
        for attempt in range(4):
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 404:
                    return None
                if r.status_code == 200 and not r.content.lstrip()[:15].lower().startswith(b"<!doctype html"):
                    return r.content
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = type(e).__name__
            self._warmup()
            time.sleep(3 * (attempt + 1))
        raise Blocked(f"{url} -> {last}")

    def api(self, path: str):
        if self.fixtures:
            p = self.fixtures / (path.strip("/").replace("/", "_") + ".json")
            return json.loads(p.read_text()) if p.exists() else None
        self._warmup()
        last = ""
        for attempt in range(4):
            try:
                r = self.s.get(HOME + path, timeout=20,
                               headers={"Accept": "application/json"})
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                last = type(e).__name__
            self._warm = False
            self._warmup()
            time.sleep(3 * (attempt + 1))
        raise Blocked(f"{path} -> {last}")


def fo_bhav_url(d: date) -> str:
    return f"{ARCHIVES}/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def cm_full_url(d: date) -> str:
    return f"{ARCHIVES}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"


def participant_url(d: date) -> str:
    return f"{ARCHIVES}/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"


# --------------------------------------------------------------------------- #
# Number helpers
# --------------------------------------------------------------------------- #
def num(x) -> float:
    if x is None:
        return float("nan")
    s = str(x).replace(",", "").strip()
    if s in ("", "-", "--", "nan"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def fmt_cr(v: float) -> str:
    """₹ crore with sign, e.g. +1,234.56"""
    return f"{v:+,.0f}" if abs(v) >= 1000 else f"{v:+,.2f}"


def fmt_k(v: float) -> str:
    """Contracts in compact form: 1.23L / 45.6K."""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}{a/1e7:.2f}Cr"
    if a >= 1e5:
        return f"{sign}{a/1e5:.2f}L"
    if a >= 1e3:
        return f"{sign}{a/1e3:.1f}K"
    return f"{sign}{a:.0f}"


def fmt_signed_k(v: float) -> str:
    return ("+" if v > 0 else "") + fmt_k(v)


def strike(v: float) -> str:
    return f"{v:.0f}" if float(v).is_integer() else f"{v:g}"


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def parse_fii_dii(data, d: date) -> dict | None:
    """NSE /api/fiidiiTradeReact -> {'fii': {...}, 'dii': {...}} for date d."""
    if not data:
        return None
    out = {}
    for row in data:
        cat = str(row.get("category", "")).upper()
        key = "fii" if "FII" in cat or "FPI" in cat else "dii" if "DII" in cat else None
        if not key:
            continue
        try:
            row_date = datetime.strptime(str(row.get("date", "")).strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        if row_date != d:
            continue
        out[key] = {"buy": num(row.get("buyValue")),
                    "sell": num(row.get("sellValue")),
                    "net": num(row.get("netValue"))}
    return out if {"fii", "dii"} <= out.keys() else None


def parse_participant(raw: bytes) -> dict:
    """fao_participant_oi_DDMMYYYY.csv -> {'FII': {col: value}, ...}."""
    text = raw.decode("utf-8", "replace").splitlines()
    start = next(i for i, ln in enumerate(text) if ln.strip().lower().startswith("client type"))
    df = pd.read_csv(io.StringIO("\n".join(text[start:])), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        name = str(r["Client Type"]).strip().upper()
        if not name or name == "NAN":
            continue
        out[name] = {c: num(r[c]) for c in df.columns if c != "Client Type"}
    return out


def read_fo_bhav(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        df = pd.read_csv(z.open(name), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    for c in ("StrkPric", "ClsPric", "PrvsClsgPric", "UndrlygPric",
              "OpnIntrst", "ChngInOpnIntrst", "SttlmPric"):
        if c in df:
            df[c] = pd.to_numeric(df[c].str.replace(",", "").str.strip(), errors="coerce")
    for c in ("FinInstrmTp", "TckrSymb", "OptnTp"):
        if c in df:
            df[c] = df[c].fillna("").str.strip().str.upper()
    df["XpryDt"] = pd.to_datetime(df["XpryDt"].str.strip(), errors="coerce").dt.date
    return df


def read_cm_full(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(lambda s: s.str.strip() if s.dtype == object else s)
    for c in ("PREV_CLOSE", "CLOSE_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS",
              "DELIV_QTY", "DELIV_PER"):
        df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce")
    return df


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def max_pain(chain: pd.DataFrame) -> float:
    strikes = sorted(chain["StrkPric"].unique())
    ce = chain[chain["OptnTp"] == "CE"][["StrkPric", "OpnIntrst"]].values
    pe = chain[chain["OptnTp"] == "PE"][["StrkPric", "OpnIntrst"]].values
    best, best_pain = strikes[0], float("inf")
    for k in strikes:
        pain = sum(oi * max(0.0, k - s) for s, oi in ce) + sum(oi * max(0.0, s - k) for s, oi in pe)
        if pain < best_pain:
            best, best_pain = k, pain
    return best


def index_options(fo: pd.DataFrame, symbol: str, d: date) -> dict | None:
    opt = fo[(fo["FinInstrmTp"] == "IDO") & (fo["TckrSymb"] == symbol)
             & (fo["OptnTp"].isin(["CE", "PE"]))].copy()
    opt = opt[opt["XpryDt"] >= d]
    if opt.empty:
        return None
    expiry = min(opt["XpryDt"])
    ch = opt[opt["XpryDt"] == expiry]
    ce, pe = ch[ch["OptnTp"] == "CE"], ch[ch["OptnTp"] == "PE"]
    ce_oi, pe_oi = ce["OpnIntrst"].sum(), pe["OpnIntrst"].sum()
    spot = ch["UndrlygPric"].dropna()
    return {
        "symbol": symbol,
        "expiry": expiry,
        "spot": float(spot.iloc[0]) if len(spot) else float("nan"),
        "pcr": pe_oi / ce_oi if ce_oi else float("nan"),
        "pcr_chg": (pe["ChngInOpnIntrst"].sum() / ce["ChngInOpnIntrst"].sum()
                    if ce["ChngInOpnIntrst"].sum() > 0 else None),
        "max_pain": max_pain(ch),
        "ce_top": ce.nlargest(3, "OpnIntrst")[["StrkPric", "OpnIntrst", "ChngInOpnIntrst"]].values.tolist(),
        "pe_top": pe.nlargest(3, "OpnIntrst")[["StrkPric", "OpnIntrst", "ChngInOpnIntrst"]].values.tolist(),
        "ce_add": ce.nlargest(3, "ChngInOpnIntrst")[["StrkPric", "ChngInOpnIntrst"]].values.tolist(),
        "pe_add": pe.nlargest(3, "ChngInOpnIntrst")[["StrkPric", "ChngInOpnIntrst"]].values.tolist(),
        "ce_oi": ce_oi, "pe_oi": pe_oi,
    }


def oi_buildup(fo: pd.DataFrame, d: date) -> dict:
    fut = fo[(fo["FinInstrmTp"] == "STF") & (fo["XpryDt"] >= d)].copy()
    if fut.empty:
        return {}
    # price from the nearest expiry, OI summed across all expiries
    near = fut.sort_values("XpryDt").groupby("TckrSymb").first()
    oi = fut.groupby("TckrSymb")[["OpnIntrst", "ChngInOpnIntrst"]].sum()
    t = near[["ClsPric", "PrvsClsgPric"]].join(oi)
    prev_oi = t["OpnIntrst"] - t["ChngInOpnIntrst"]
    t = t[(prev_oi > 0) & (t["PrvsClsgPric"] > 0)].copy()
    t["px"] = (t["ClsPric"] / t["PrvsClsgPric"] - 1) * 100
    t["oi"] = t["ChngInOpnIntrst"] / (t["OpnIntrst"] - t["ChngInOpnIntrst"]) * 100
    groups = {
        "Long buildup": t[(t.px > 0) & (t.oi > 0)].sort_values("oi", ascending=False),
        "Short buildup": t[(t.px < 0) & (t.oi > 0)].sort_values("oi", ascending=False),
        "Short covering": t[(t.px > 0) & (t.oi < 0)].sort_values("oi"),
        "Long unwinding": t[(t.px < 0) & (t.oi < 0)].sort_values("oi"),
    }
    return {k: v.head(TOP_N)[["px", "oi"]].reset_index().values.tolist()
            for k, v in groups.items()} | {"_counts": {k: len(v) for k, v in groups.items()}}


def advance_decline(cm: pd.DataFrame, fno: set[str]) -> dict:
    eq = cm[(cm["SERIES"] == "EQ") & cm["PREV_CLOSE"].gt(0) & cm["CLOSE_PRICE"].notna()]

    def count(df):
        a = int((df.CLOSE_PRICE > df.PREV_CLOSE).sum())
        dcl = int((df.CLOSE_PRICE < df.PREV_CLOSE).sum())
        u = int((df.CLOSE_PRICE == df.PREV_CLOSE).sum())
        return {"adv": a, "dec": dcl, "unch": u, "ratio": a / dcl if dcl else float("inf")}

    out = {"all": count(eq)}
    if fno:
        out["fno"] = count(eq[eq["SYMBOL"].isin(fno)])
    return out


def delivery(cm: pd.DataFrame, fno: set[str], history: dict) -> tuple[dict, dict]:
    """Returns (result, updated_history). history: {symbol: [deliv_qty, ...]}."""
    eq = cm[(cm["SERIES"] == "EQ") & cm["DELIV_QTY"].notna() & cm["DELIV_PER"].notna()]
    if fno:
        eq = eq[eq["SYMBOL"].isin(fno)]
    new_hist = {}
    spikes = []
    for _, r in eq.iterrows():
        sym, q = r["SYMBOL"], float(r["DELIV_QTY"])
        past = history.get(sym, [])
        liquid = r["TURNOVER_LACS"] >= MIN_TURNOVER_CR * 100
        if len(past) >= 3 and liquid:
            avg = sum(past) / len(past)
            if avg > 0 and q / avg >= DELIVERY_SPIKE_X and r["DELIV_PER"] >= DELIVERY_MIN_PCT:
                spikes.append([sym, float(r["DELIV_PER"]), q / avg,
                               (r["CLOSE_PRICE"] / r["PREV_CLOSE"] - 1) * 100])
        new_hist[sym] = (past + [q])[-HISTORY_DAYS:]
    spikes.sort(key=lambda x: x[2], reverse=True)
    liquid = eq[eq["TURNOVER_LACS"] >= MIN_TURNOVER_CR * 100].copy()
    liquid["chg"] = (liquid["CLOSE_PRICE"] / liquid["PREV_CLOSE"] - 1) * 100
    top = liquid.nlargest(TOP_N, "DELIV_PER")[["SYMBOL", "DELIV_PER", "chg"]].values.tolist()
    days = max((len(v) for v in history.values()), default=0)
    return {"spikes": spikes[:TOP_N], "top": top, "history_days": days}, new_hist


# --------------------------------------------------------------------------- #
# Discord embeds
# --------------------------------------------------------------------------- #
GREEN, RED, BLUE, GOLD, GREY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xF1C40F, 0x95A5A6


def code(lines: list[str]) -> str:
    return "\n```\n" + "\n".join(lines) + "\n```\n"


def embed_fii_dii(fd: dict | None, mtd: dict) -> dict:
    if not fd:
        return {"title": "💰 FII / DII cash", "color": GREY,
                "description": "Not available yet (NSE hadn't published it or blocked the request)."}
    lines = [f"{'':5}{'Buy':>9}{'Sell':>9}{'Net':>9}"]
    for k in ("fii", "dii"):
        v = fd[k]
        lines.append(f"{k.upper():5}{v['buy']:>9,.0f}{v['sell']:>9,.0f}{v['net']:>+9,.0f}")
    desc = "₹ crore, provisional\n" + code(lines)
    if mtd:
        note = "" if mtd["complete"] else f" (tracked since {mtd['since']:%d %b})"
        desc += (f"**Month to date{note}**\n"
                 f"FII {fmt_cr(mtd['fii'])} cr · DII {fmt_cr(mtd['dii'])} cr")
    net = fd["fii"]["net"] + fd["dii"]["net"]
    return {"title": "💰 FII / DII cash market", "color": GREEN if net >= 0 else RED,
            "description": desc}


def embed_participant(today: dict | None, prev: dict | None) -> dict:
    if not today or "FII" not in today:
        return {"title": "🧾 Participant-wise OI", "color": GREY,
                "description": "Not published yet."}
    f = today["FII"]
    L, S = f["Future Index Long"], f["Future Index Short"]
    long_pct = L / (L + S) * 100 if L + S else float("nan")
    desc = f"**FII index futures:** {long_pct:.1f}% long / {100-long_pct:.1f}% short"
    if prev and "FII" in prev:
        p = prev["FII"]
        pl = p["Future Index Long"] / (p["Future Index Long"] + p["Future Index Short"]) * 100
        desc += f" ({long_pct - pl:+.1f} pts)"
        desc += (f"\nLongs {fmt_signed_k(L - p['Future Index Long'])}, "
                 f"shorts {fmt_signed_k(S - p['Future Index Short'])} contracts")
    desc += "\n\n**Index futures net (long − short)**"
    lines = [f"{'':7}{'Net':>9}{'Chg':>9}"]
    for who in ("FII", "DII", "PRO", "CLIENT"):
        r = today.get(who)
        if not r:
            continue
        net = r["Future Index Long"] - r["Future Index Short"]
        chg = ""
        if prev and who in prev:
            pr = prev[who]
            chg = fmt_signed_k(net - (pr["Future Index Long"] - pr["Future Index Short"]))
        label = {"FII": "FII", "DII": "DII", "PRO": "Pro", "CLIENT": "Client"}[who]
        lines.append(f"{label:7}{fmt_k(net):>9}{chg:>9}")
    desc += code(lines)
    desc += "**Index options net (FII)**\n"
    calls = f["Option Index Call Long"] - f["Option Index Call Short"]
    puts = f["Option Index Put Long"] - f["Option Index Put Short"]
    desc += f"Calls {fmt_k(calls)} · Puts {fmt_k(puts)}"
    color = GREEN if long_pct >= 50 else RED
    return {"title": "🧾 Participant-wise OI", "color": color, "description": desc}


def embed_index(o: dict | None, symbol: str) -> dict:
    if not o:
        return {"title": f"📈 {symbol} options", "color": GREY, "description": "No data."}
    lines = ["Highest OI  Strike      OI     Chg"]
    for tag, rows in (("CE", o["ce_top"]), ("PE", o["pe_top"])):
        for k, oi, chg in rows:
            lines.append(f"  {tag:8}{strike(k):>8}{fmt_k(oi):>8}{fmt_signed_k(chg):>8}")
    lines.append("")
    lines.append("Most OI added  Strike     Chg")
    for tag, rows in (("CE", o["ce_add"]), ("PE", o["pe_add"])):
        for k, chg in rows[:2]:
            lines.append(f"  {tag:11}{strike(k):>8}{fmt_signed_k(chg):>8}")
    desc = (f"Close **{o['spot']:,.2f}** · Expiry **{o['expiry']:%d %b}**\n"
            f"PCR **{o['pcr']:.2f}** · Max pain **{strike(o['max_pain'])}**"
            + code(lines))
    color = GREEN if o["pcr"] >= 1 else RED if o["pcr"] < 0.8 else GOLD
    return {"title": f"📈 {symbol} options", "color": color, "description": desc}


def embed_buildup(b: dict) -> dict:
    if not b:
        return {"title": "🏗️ OI buildup (stock futures)", "color": GREY, "description": "No data."}
    icons = {"Long buildup": "🟢", "Short buildup": "🔴",
             "Short covering": "🔵", "Long unwinding": "🟠"}
    parts = []
    for k, rows in b.items():
        if k.startswith("_"):
            continue
        head = f"{icons[k]} **{k}** ({b['_counts'][k]})"
        if not rows:
            parts.append(head + "\nNone")
            continue
        lines = [f"{sym[:11]:11}{px:>+7.2f}%{oi:>+7.1f}%" for sym, px, oi in rows]
        parts.append(head + code([f"{'Stock':11}{'Price':>8}{'OI':>8}"] + lines))
    return {"title": "🏗️ OI buildup (stock futures)", "color": BLUE,
            "description": "\n".join(parts)}


def embed_delivery(dv: dict | None) -> dict:
    if not dv:
        return {"title": "📦 Delivery", "color": GREY, "description": "No data."}
    desc = ""
    if dv["history_days"] >= 3:
        if dv["spikes"]:
            lines = [f"{s[:10]:10}{p:>5.0f}%{x:>5.1f}x{c:>+7.2f}%" for s, p, x, c in dv["spikes"]]
            desc += ("**Delivery spikes** (vs recent avg)"
                     + code([f"{'Stock':10}{'Del%':>6}{'vsAvg':>6}{'Price':>8}"] + lines))
        else:
            desc += "No unusual delivery spikes today.\n"
    else:
        desc += f"_Spike detection starts after 3 days of history ({dv['history_days']}/3)._\n"
    if dv["top"]:
        lines = [f"{s[:11]:11}{p:>6.1f}%{c:>+8.2f}%" for s, p, c in dv["top"]]
        desc += (f"**Highest delivery %** (F&O, ≥₹{MIN_TURNOVER_CR} cr traded)"
                 + code([f"{'Stock':11}{'Del%':>7}{'Price':>9}"] + lines))
    return {"title": "📦 Delivery", "color": GOLD, "description": desc}


def embed_ad(ad: dict | None) -> dict:
    if not ad:
        return {"title": "⚖️ Advance / Decline", "color": GREY, "description": "No data."}
    lines = [f"{'':9}{'Adv':>6}{'Dec':>6}{'Ratio':>7}"]
    for key, label in (("all", "All EQ"), ("fno", "F&O")):
        if key in ad:
            r = ad[key]
            ratio = "∞" if r["ratio"] == float("inf") else f"{r['ratio']:.2f}"
            lines.append(f"{label:9}{r['adv']:>6}{r['dec']:>6}{ratio:>7}")
    r = ad["all"]
    color = GREEN if r["adv"] >= r["dec"] else RED
    return {"title": "⚖️ Advance / Decline", "color": color, "description": code(lines)}


def batches(embeds: list[dict], max_chars: int = 5500) -> list[list[dict]]:
    """Discord: ≤10 embeds and ≤6000 characters per message."""
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


def post_discord(webhook: str, content: str, embeds: list[dict]) -> None:
    for i, group in enumerate(batches(embeds)):
        payload = {"embeds": group, "allowed_mentions": {"parse": []}}
        if i == 0:
            payload["content"] = content
        for attempt in range(3):
            r = requests.post(webhook, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            r.raise_for_status()
            break
        time.sleep(1)


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


def month_to_date(st: dict, d: date, fd: dict | None) -> dict:
    daily = st.setdefault("fii_dii_daily", {})
    if fd:
        daily[d.isoformat()] = {"fii": fd["fii"]["net"], "dii": fd["dii"]["net"]}
    # keep ~3 months
    cutoff = (d - timedelta(days=95)).isoformat()
    for k in [k for k in daily if k < cutoff]:
        del daily[k]
    month = [(k, v) for k, v in daily.items() if k[:7] == d.isoformat()[:7] and k <= d.isoformat()]
    if not month:
        return {}
    since = date.fromisoformat(min(k for k, _ in month))
    # Only a full month if the bot was already running before this month began.
    complete = st.get("tracking_started", d.isoformat()) < d.replace(day=1).isoformat()
    return {"fii": sum(v["fii"] for _, v in month), "dii": sum(v["dii"] for _, v in month),
            "since": since, "complete": complete}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def previous_participant(nse: NSE, d: date, st: dict):
    cached = st.get("participant", {})
    prior = sorted(k for k in cached if k < d.isoformat())
    if prior:
        return cached[prior[-1]]
    for back in range(1, 8):
        pd_ = d - timedelta(days=back)
        if pd_.weekday() >= 5:
            continue
        raw = nse.file(participant_url(pd_))
        if raw:
            return parse_participant(raw)
    return None


def run(d: date, dry_run: bool, final: bool, force: bool, fixtures: Path | None) -> int:
    st = load_state()
    st.setdefault("tracking_started", d.isoformat())
    if st.get("last_posted") == d.isoformat() and not force:
        print(f"Already posted for {d}, nothing to do.")
        return 0

    nse = NSE(fixtures)
    problems = []

    def grab(label, fn):
        try:
            return fn()
        except Blocked as e:
            problems.append(f"{label}: blocked ({e})")
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't kill the post
            problems.append(f"{label}: {type(e).__name__}: {e}")
        return None

    fo_raw = grab("F&O bhavcopy", lambda: nse.file(fo_bhav_url(d)))
    cm_raw = grab("CM bhavcopy", lambda: nse.file(cm_full_url(d)))
    part_raw = grab("Participant OI", lambda: nse.file(participant_url(d)))
    fd = grab("FII/DII", lambda: parse_fii_dii(nse.api("/api/fiidiiTradeReact"), d))

    blocked = any("blocked" in p for p in problems)
    if blocked and fo_raw is None and cm_raw is None:
        for p in problems:
            print("ERROR:", p)
        print("NSE blocked the main files, so nothing was posted. The next run will retry.")
        return 1
    if fo_raw is None and cm_raw is None and not blocked:
        if final:
            print(f"No NSE bhavcopy for {d}: market holiday (or not published). Skipping.")
            return 0
        print("Bhavcopies not published yet; will try again on the later run.")
        return 0
    if not final and (part_raw is None or fd is None or fo_raw is None or cm_raw is None):
        print("Some data isn't out yet; waiting for the later run. Missing:",
              [n for n, v in (("F&O", fo_raw), ("CM", cm_raw),
                              ("participant", part_raw), ("FII/DII", fd)) if v is None])
        for p in problems:
            print("  -", p)
        return 0

    fo = grab("F&O parse", lambda: read_fo_bhav(fo_raw)) if fo_raw else None
    cm = grab("CM parse", lambda: read_cm_full(cm_raw)) if cm_raw else None
    part = grab("Participant parse", lambda: parse_participant(part_raw)) if part_raw else None
    prev_part = grab("Previous participant", lambda: previous_participant(nse, d, st)) if part else None

    fno = set(fo.loc[fo["FinInstrmTp"] == "STF", "TckrSymb"]) if fo is not None else set()
    idx = {s: grab(f"{s} options", lambda s=s: index_options(fo, s, d)) if fo is not None else None
           for s in INDICES}
    build = grab("OI buildup", lambda: oi_buildup(fo, d)) if fo is not None else None
    ad = grab("Advance/decline", lambda: advance_decline(cm, fno)) if cm is not None else None
    dv = None
    if cm is not None:
        res = grab("Delivery", lambda: delivery(cm, fno, st.get("delivery_hist", {})))
        if res:
            dv, new_hist = res
            st["delivery_hist"] = new_hist
    mtd = month_to_date(st, d, fd)

    embeds = [
        embed_fii_dii(fd, mtd),
        embed_participant(part, prev_part),
        *(embed_index(idx[s], s) for s in INDICES),
        embed_buildup(build),
        embed_delivery(dv),
        embed_ad(ad),
    ]
    for e in embeds:
        e["description"] = e["description"].strip()
    embeds[-1]["footer"] = {"text": "Source: NSE end-of-day data · Not investment advice"}
    content = f"📊 **Indian Market Wrap — {d:%a %d %b %Y}**"

    if dry_run:
        print("\n" + content)
        for e in embeds:
            print(f"\n## {e['title']}\n{e['description']}")
        print(f"\n[{len(batches(embeds))} Discord message(s)]")
    else:
        webhook = os.environ.get("DISCORD_WEBHOOK_INDIA_DATA", "").strip()
        if not webhook:
            sys.exit("DISCORD_WEBHOOK_INDIA_DATA secret is not set")
        post_discord(webhook, content, embeds)
        print("Posted to Discord.")

    if part:
        cache = st.setdefault("participant", {})
        cache[d.isoformat()] = part
        for k in sorted(cache)[:-5]:
            del cache[k]
    st["last_posted"] = d.isoformat()
    if not dry_run or fixtures:
        save_state(st)

    for p in problems:
        print("WARNING:", p)
    # Fail the run (so GitHub emails you) only if NSE blocked a core file.
    return 1 if blocked and (fo is None or cm is None) else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--final", action="store_true", help="post even if some data is missing")
    ap.add_argument("--force", action="store_true", help="post again even if already posted")
    ap.add_argument("--fixtures", type=Path)
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(IST).date()
    if d.weekday() >= 5 and not a.force:
        print(f"{d} is a weekend, skipping.")
        return
    sys.exit(run(d, a.dry_run, a.final, a.force, a.fixtures))


if __name__ == "__main__":
    main()
