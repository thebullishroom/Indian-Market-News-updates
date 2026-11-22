"""Offline tests: python tests/test_bot.py"""
import json, os, subprocess, sys, tempfile, threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import india_data as bot  # noqa: E402
import make_fixtures  # noqa: E402

DAYS = [date(2026, 11, d) for d in (16, 17, 18, 19, 20)]


def run(args, state, fx, env_extra=None):
    env = dict(os.environ, INDIA_STATE_FILE=str(state), **(env_extra or {}))
    return subprocess.run([sys.executable, str(ROOT / "india_data.py"), "--fixtures", str(fx), *args],
                          capture_output=True, text=True, env=env)


def test_max_pain():
    chain = pd.DataFrame({"StrkPric": [100, 110, 120] * 2, "OptnTp": ["CE"] * 3 + ["PE"] * 3,
                          "OpnIntrst": [10, 100, 10, 10, 10, 100]})
    # pain at 100: puts 10*10+100*20=2100; at 110: ce 10*10 + pe 100*10 = 1100; at 120: ce 10*20+100*10=1200, pe 0
    assert bot.max_pain(chain) == 110


def test_buildup_classification(fx):
    fo = bot.read_fo_bhav((fx / "BhavCopy_NSE_FO_0_0_0_20261120_F_0000.csv.zip").read_bytes())
    b = bot.oi_buildup(fo, DAYS[-1])
    for sym, px, oi in b["Long buildup"]:
        assert px > 0 and oi > 0
    for sym, px, oi in b["Short buildup"]:
        assert px < 0 and oi > 0
    for sym, px, oi in b["Short covering"]:
        assert px > 0 and oi < 0
    for sym, px, oi in b["Long unwinding"]:
        assert px < 0 and oi < 0
    assert sum(b["_counts"].values()) == len(make_fixtures.STOCKS)


def test_expired_ignored(fx):
    fo = bot.read_fo_bhav((fx / "BhavCopy_NSE_FO_0_0_0_20261120_F_0000.csv.zip").read_bytes())
    o = bot.index_options(fo, "NIFTY", DAYS[-1])
    assert o["expiry"] == date(2026, 11, 24)
    assert all(oi < 9e8 for _, oi, _ in o["ce_top"])


def test_flows(fx, tmp):
    st = tmp / "s.json"
    # missing data on first run -> waits, no post
    r = run(["--date", "2026-11-16", "--dry-run"], st, fx)
    assert "waiting for the later run" in r.stdout and r.returncode == 0, r.stdout
    # holiday (no files) on final run -> skip
    r = run(["--date", "2026-11-24", "--dry-run", "--final"], st, fx)
    assert "holiday" in r.stdout and r.returncode == 0
    # weekend
    r = run(["--date", "2026-11-21", "--dry-run"], st, fx)
    assert "weekend" in r.stdout
    for d in DAYS:
        r = run(["--date", d.isoformat(), "--dry-run", "--final"], st, fx)
        assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr
    out = r.stdout
    assert "Delivery spikes" in out and "SBIN" in out
    assert "tracked since 20 Nov" in out
    # already posted -> skip
    r = run(["--date", "2026-11-20", "--dry-run", "--final"], st, fx)
    assert "Already posted" in r.stdout
    state = json.loads(st.read_text())
    assert len(state["participant"]) == 5 and state["last_posted"] == "2026-11-20"


def test_discord_post(fx, tmp):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204); self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = tmp / "post.json"
    r = run(["--date", "2026-11-20", "--final"], st, fx,
            {"DISCORD_WEBHOOK_INDIA_DATA": f"http://127.0.0.1:{srv.server_port}/hook",
             "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
    srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    assert got and got[0]["content"].startswith("📊")
    for msg in got:
        assert len(msg["embeds"]) <= 10
        total = sum(len(e["title"]) + len(e["description"]) for e in msg["embeds"])
        assert total < 6000
        for e in msg["embeds"]:
            assert len(e["description"]) <= 4096 and len(e["title"]) <= 256
            assert e["description"].count("```") % 2 == 0
    print(f"  Discord got {len(got)} message(s), {sum(len(m['embeds']) for m in got)} embeds")


def test_blocked(tmp):
    class B(bot.NSE):
        def file(self, url):
            raise bot.Blocked(url + " -> HTTP 403")

        def api(self, path):
            raise bot.Blocked(path + " -> HTTP 403")

    orig = bot.NSE
    bot.NSE = lambda fixtures=None: B(None)
    bot.STATE_FILE = tmp / "blocked.json"
    try:
        rc = bot.run(DAYS[-1], dry_run=True, final=True, force=False, fixtures=None)
    finally:
        bot.NSE = orig
    assert rc == 1  # run marked failed so GitHub emails
    assert not bot.STATE_FILE.exists()  # not marked as posted, so the later run retries


def test_partial_fii_missing(fx, tmp):
    import shutil
    fx2 = tmp / "fx2"
    shutil.copytree(fx, fx2)
    (fx2 / "api_fiidiiTradeReact.json").unlink()
    (fx2 / "fao_participant_oi_20112026.csv").unlink()
    r = run(["--date", "2026-11-20", "--dry-run", "--final"], tmp / "p.json", fx2)
    assert r.returncode == 0 and "Not available yet" in r.stdout and "Not published yet" in r.stdout
    assert "NIFTY options" in r.stdout


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fx = tmp / "fx"
        make_fixtures.main(fx, DAYS)
        tests = [("max pain", lambda: test_max_pain()),
                 ("OI buildup classes", lambda: test_buildup_classification(fx)),
                 ("expired contracts ignored", lambda: test_expired_ignored(fx)),
                 ("wait / holiday / weekend / repeat flows", lambda: test_flows(fx, tmp)),
                 ("Discord post + limits", lambda: test_discord_post(fx, tmp)),
                 ("NSE blocked", lambda: test_blocked(tmp)),
                 ("partial data still posts", lambda: test_partial_fii_missing(fx, tmp))]
        for name, fn in tests:
            fn()
            print("PASS", name)
