"""Create sample files in NSE's real formats so the bot can be tested offline."""
import io
import json
import random
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

FO_COLS = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
           "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
           "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
           "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")
STOCKS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK", "ITC",
          "LT", "KOTAKBANK", "BHARTIARTL", "MARUTI", "TATAMOTORS", "SUNPHARMA", "WIPRO",
          "HCLTECH", "BAJFINANCE", "ADANIENT", "TITAN", "ONGC", "NTPC", "POWERGRID",
          "COALINDIA", "JSWSTEEL", "TATASTEEL", "HINDALCO", "DLF", "BANKBARODA", "PNB", "VEDL"]
NON_FNO = [f"SMALLCO{i}" for i in range(40)]


def fo_bhav(d: date, rnd: random.Random) -> bytes:
    rows = [FO_COLS]
    tr = d.isoformat()

    def row(tp, sym, exp, k, opt, cls, prev, und, oi, chg):
        return (f"{tr},{tr},FO,NSE,{tp},1,,{sym},,{exp},{exp},{k},{opt},{sym}X,"
                f"{cls},{cls},{cls},{cls},{cls},{prev},{und},{cls},{oi},{chg},100,1,1,F1,75,,,,,")

    for sym, spot, step, exps in (("NIFTY", 26050.4, 50, [d + timedelta(days=4), d + timedelta(days=11)]),
                                  ("BANKNIFTY", 58830.15, 100, [d + timedelta(days=11)])):
        # an expired contract that must be ignored
        rows.append(row("IDO", sym, (d - timedelta(days=3)).isoformat(), spot, "CE", 1, 1, spot, 999999999, 0))
        atm = round(spot / step) * step
        for exp in exps:
            for i in range(-20, 21):
                k = atm + i * step
                ce_oi = int(rnd.randint(2, 10) * 1e5 * (3 if i == 4 else 1))
                pe_oi = int(rnd.randint(2, 10) * 1e5 * (3 if i == -6 else 1))
                rows.append(row("IDO", sym, exp.isoformat(), k, "CE", 50, 60, spot, ce_oi, rnd.randint(-2, 4) * 10000))
                rows.append(row("IDO", sym, exp.isoformat(), k, "PE", 50, 40, spot, pe_oi, rnd.randint(-2, 4) * 10000))
        rows.append(row("IDF", sym, exps[-1].isoformat(), "", "", spot, spot - 80, spot, 150000, 2000))
    near, nxt = d + timedelta(days=4), d + timedelta(days=39)
    for s in STOCKS:
        prev = rnd.uniform(200, 4000)
        cls = prev * (1 + rnd.uniform(-0.04, 0.04))
        oi = rnd.randint(1_000_000, 50_000_000)
        chg = int(oi * rnd.uniform(-0.08, 0.1))
        rows.append(row("STF", s, near.isoformat(), "", "", round(cls, 2), round(prev, 2), cls, oi, chg))
        rows.append(row("STF", s, nxt.isoformat(), "", "", round(cls * 1.005, 2), round(prev, 2), cls, oi // 10, chg // 10))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv", "\n".join(rows) + "\n")
    return buf.getvalue()


def cm_full(d: date, rnd: random.Random, spike: set) -> bytes:
    # real file has a space after every comma
    lines = ["SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
             "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"]
    for s in STOCKS + NON_FNO:
        prev = rnd.uniform(100, 3000)
        cls = round(prev * (1 + rnd.uniform(-0.03, 0.03)), 2)
        qty = rnd.randint(1_000_000, 5_000_000)
        dq = int(qty * rnd.uniform(0.25, 0.45))
        if s in spike:
            qty = int(qty * 1.6)
            dq = int(qty * 0.78)
        turnover = qty * cls / 1e5
        lines.append(f"{s}, EQ, {d:%d-%b-%Y}, {prev:.2f}, {prev:.2f}, {cls:.2f}, {cls:.2f}, {cls:.2f}, "
                     f"{cls:.2f}, {cls:.2f}, {qty}, {turnover:.2f}, 1000, {dq}, {dq / qty * 100:.2f}")
    lines.append(f"GOLDBEES, BE, {d:%d-%b-%Y}, 50, 50, 50, 50, 50, 50, 50, 100, 0.05, 3, -, -")
    return ("\n".join(lines) + "\n").encode()


def participant(d: date, fii_long: int, fii_short: int) -> bytes:
    hdr = ("Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short\t,"
           "Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,"
           "Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,"
           "Total Long Contracts\t,Total Short Contracts")
    rows = [
        f"Client,180000,95000,2500000,350000,3200000,2900000,2700000,3300000,500000,300000,450000,250000,9000000,8500000",
        f"DII,75000,12000,200000,3100000,500,0,0,0,0,0,0,0,280000,3110000",
        f"FII,{fii_long},{fii_short},2900000,1700000,700000,650000,600000,520000,90000,60000,80000,50000,4400000,3500000",
        f"Pro,40000,70000,600000,1200000,1300000,1100000,1600000,1200000,200000,150000,250000,120000,3400000,3900000",
        f"TOTAL,400000,400000,6200000,6200000,5200000,4650000,4900000,5020000,790000,510000,780000,420000,17080000,19010000",
    ]
    title = f'"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on {d:%b %d, %Y}"'
    return ("\n".join([title, hdr] + rows) + "\n").encode()


def fii_dii(d: date) -> list:
    return [
        {"category": "DII **", "date": f"{d:%d-%b-%Y}", "buyValue": "15234.56", "sellValue": "11890.12", "netValue": "3344.44"},
        {"category": "FII/FPI *", "date": f"{d:%d-%b-%Y}", "buyValue": "11876.43", "sellValue": "14010.88", "netValue": "-2134.45"},
    ]


def main(out: Path, days: list[date]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for i, d in enumerate(days):
        rnd = random.Random(d.toordinal())
        (out / f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip").write_bytes(fo_bhav(d, rnd))
        spike = {"SBIN", "ITC"} if i == len(days) - 1 else set()
        (out / f"sec_bhavdata_full_{d:%d%m%Y}.csv").write_bytes(cm_full(d, rnd, spike))
        (out / f"fao_participant_oi_{d:%d%m%Y}.csv").write_bytes(participant(d, 95000 + i * 4000, 210000 - i * 3000))
    (out / "api_fiidiiTradeReact.json").write_text(json.dumps(fii_dii(days[-1])))


if __name__ == "__main__":
    out = Path(sys.argv[1])
    days = [date.fromisoformat(x) for x in sys.argv[2:]]
    main(out, days)
