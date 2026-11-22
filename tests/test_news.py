"""Offline tests for the news bot: python tests/test_news.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import india_news as bot  # noqa: E402
import make_news_fixtures  # noqa: E402


def run(args, state, fx, **env):
    e = dict(os.environ, INDIA_NEWS_STATE_FILE=str(state), NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", **env)
    return subprocess.run([sys.executable, str(ROOT / "india_news.py"), *args, "--fixtures", str(fx)],
                          capture_output=True, text=True, env=e)


def test_classify():
    c = bot.classify
    assert c("Financial Result Updates", "") == ["Results"]
    assert c("Board Meeting Intimation", "to consider results and dividend") == []
    assert c("Outcome of Board Meeting", "approved unaudited financial results") == ["Results"]
    assert c("Outcome of Board Meeting", "declared interim dividend of Rs 5") == ["Dividend"]
    assert c("Updates", "Dividend Distribution Policy revised") == []
    assert c("Outcome", "approved bonus issue in the ratio 1:1") == ["Bonus"]
    assert c("Outcome", "approved sub-division of equity shares from Rs 10 to Rs 2") == ["Split"]
    assert c("General", "Board meeting will be held to consider a stock split") == []
    assert c("Financial Results", "Results", "Result") == ["Results"]
    assert c("Board Meeting Intimation", "", "Board Meeting Intimation") == []


def test_helpers():
    from datetime import date
    assert bot.parse_d("23-Nov-2026") == date(2026, 11, 23)
    assert bot.parse_d("23 Nov 2026") == date(2026, 11, 23)
    assert bot.parse_dt("2026-11-23T10:06:01.887").hour == 10
    assert bot.norm_name("Reliance Industries Ltd.") == bot.norm_name("Reliance Industries Limited")
    assert bot.norm_name("M&M Financial Ltd") == bot.norm_name("M and M Financial Limited")


def test_weekly(fx, tmp):
    r = run(["weekly", "--date", "2026-11-22", "--dry-run"], tmp / "w.json", fx)
    out = r.stdout
    assert r.returncode == 0, r.stderr
    assert "23 Nov to 29 Nov 2026" in out
    assert out.count("RELIANCE") == 1, "RELIANCE from NSE and BSE should be merged"
    assert "Market holiday" in out and "Guru Nanak" in out
    assert "IIP" in out and "Saturday, 28 Nov" in out
    assert "TCS" not in out, "'Other business matters' meetings are skipped"
    assert "SBIN (Fund Raising)" in out
    # running again doesn't repost
    r = run(["weekly", "--date", "2026-11-22", "--dry-run"], tmp / "w.json", fx)
    assert "already posted" in r.stdout
    # a midweek run previews the following week
    r = run(["weekly", "--date", "2026-11-25", "--dry-run"], tmp / "w2.json", fx)
    assert "30 Nov to 06 Dec 2026" in r.stdout and "GDP Q2" in r.stdout and "RBI monetary policy decision" in r.stdout


def test_morning_and_alerts(fx, tmp):
    st = tmp / "m.json"
    r = run(["morning", "--date", "2026-11-23", "--dry-run"], st, fx, NEWS_NOW="2026-11-23T10:30")
    out = r.stdout
    assert r.returncode == 0, r.stderr
    assert "Morning Brief — Mon 23 Nov 2026" in out and "ITC div ₹6.50" in out
    assert "4 new alert(s)" in out, out
    assert out.count("Results · RELIANCE") == 1           # NSE + BSE copies -> one alert
    assert "INFY" not in out.split("Checked")[1]          # intimation, not an alert
    assert "SMALLCAP1" not in out.split("Checked")[1]     # not an F&O stock
    assert "TCS" not in out                               # older than first-run lookback
    assert "Results + Split · WIPRO" in out
    # second run: nothing new
    r = run(["alerts", "--date", "2026-11-23", "--dry-run"], st, fx, NEWS_NOW="2026-11-23T10:45")
    assert "0 new alert(s)" in r.stdout
    # morning brief not repeated
    r = run(["morning", "--date", "2026-11-23", "--dry-run"], st, fx, NEWS_NOW="2026-11-23T11:00")
    assert "already posted" in r.stdout
    # holiday + weekend
    assert "no live alerts" in run(["alerts", "--date", "2026-11-24", "--dry-run"], st, fx).stdout
    assert "Weekend" in run(["morning", "--date", "2026-11-28", "--dry-run"], st, fx).stdout


def test_all_universe(fx, tmp):
    r = run(["alerts", "--date", "2026-11-23", "--dry-run"], tmp / "a.json", fx,
            NEWS_NOW="2026-11-23T10:30", NEWS_UNIVERSE="all")
    assert "SMALLCAP1" in r.stdout and "Bse Only Industries" in r.stdout


def test_nse_blocked(fx, tmp):
    fx2 = tmp / "nse_down"
    shutil.copytree(fx, fx2)
    for f in fx2.glob("nse_*.json"):
        f.unlink()
    r = run(["morning", "--date", "2026-11-23", "--dry-run"], tmp / "b.json", fx2, NEWS_NOW="2026-11-23T10:30")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "BSE only (NSE unavailable)" in r.stdout
    assert "RELIANCE" in r.stdout and "Bonus · TATASTEEL" in r.stdout
    assert "Market holiday" not in r.stdout
    assert r.stdout.count("NSE holidays:") == 1  # falls back to built-in list, tried once


def test_all_blocked(tmp):
    empty = tmp / "empty"
    empty.mkdir()
    r = run(["alerts", "--date", "2026-11-23", "--dry-run"], tmp / "c.json", empty)
    assert r.returncode == 1 and "both failed" in r.stdout
    r = run(["weekly", "--date", "2026-11-22", "--dry-run"], tmp / "c.json", empty)
    assert r.returncode == 1


def test_flood_and_discord(fx, tmp):
    """300 result filings with universe=all: capped, batched, within Discord limits."""
    fx3 = tmp / "flood"
    shutil.copytree(fx, fx3)
    big = [{"symbol": f"CO{i}", "sm_name": f"Company {i} Limited", "desc": "Financial Result Updates",
            "attchmntText": "Unaudited financial results " + "x" * 600,
            "sort_date": f"2026-11-23 10:{i % 60:02d}:00", "seq_id": str(5000 + i)} for i in range(300)]
    (fx3 / "nse_announcements.json").write_text(json.dumps(big))
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{srv.server_port}/hook"
    r = run(["morning", "--date", "2026-11-23"], tmp / "f.json", fx3, NEWS_NOW="2026-11-23T11:30",
            NEWS_UNIVERSE="all", DISCORD_WEBHOOK_INDIA_NEWS=hook)
    srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    embeds = [e for m in got for e in m["embeds"]]
    assert got[0]["content"].startswith("☀️")
    assert len(embeds) == 1 + bot.MAX_ALERTS_PER_RUN
    assert any("more on NSE/BSE" in m.get("content", "") for m in got)
    for m in got:
        assert len(m["embeds"]) <= 10
        assert sum(len(e["title"]) + len(e["description"]) + len(e.get("footer", {}).get("text", ""))
                   for e in m["embeds"]) < 6000
        for e in m["embeds"]:
            assert len(e["title"]) <= 256 and len(e["description"]) <= 4096
    assert all(m["allowed_mentions"] == {"parse": []} for m in got)
    print(f"  Discord got {len(got)} messages, {len(embeds)} embeds")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fx = tmp / "fx"
        make_news_fixtures.write(fx)
        tests = [("classify filings", test_classify),
                 ("dates and names", test_helpers),
                 ("weekly preview", lambda: test_weekly(fx, tmp)),
                 ("morning brief + alerts + no repeats", lambda: test_morning_and_alerts(fx, tmp)),
                 ("all-companies mode", lambda: test_all_universe(fx, tmp)),
                 ("NSE down, BSE keeps going", lambda: test_nse_blocked(fx, tmp)),
                 ("both exchanges down", lambda: test_all_blocked(tmp)),
                 ("flood cap + Discord limits", lambda: test_flood_and_discord(fx, tmp))]
        for name, fn in tests:
            fn()
            print("PASS", name)
