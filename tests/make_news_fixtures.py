"""Sample NSE/BSE responses (in the exchanges' JSON shapes) for offline news-bot tests."""
import json
import sys
from pathlib import Path


def write(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    j = lambda name, data: (out / name).write_text(json.dumps(data))  # noqa: E731

    (out / "fo_mktlots.csv").write_text(
        "UNDERLYING                                        ,SYMBOL    ,NOV-26    ,DEC-26\n"
        "NIFTY 50                                          ,NIFTY     ,75        ,75\n"
        + "".join(f"{n:50},{s:10},500       ,500\n" for s, n in FNO.items()))
    (out / "EQUITY_L.csv").write_text(
        "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
        + "".join(f"{s},{n},EQ,01-JAN-2000,10,1,INE000000000,10\n" for s, n in {**FNO, **OTHER}.items()))

    j("nse_holidays.json", {"CM": [
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day"},
        {"tradingDate": "24-Nov-2026", "weekDay": "Tuesday", "description": "Prakash Gurpurb Sri Guru Nanak Dev"},
        {"tradingDate": "25-Dec-2026", "weekDay": "Friday", "description": "Christmas"}]})

    j("nse_board_meetings.json", [
        {"bm_symbol": "RELIANCE", "sm_name": "Reliance Industries Limited", "bm_purpose": "Financial Results",
         "bm_desc": "To consider and approve the financial results for the period ended September 30, 2026",
         "bm_date": "23-Nov-2026"},
        {"bm_symbol": "SMALLCAP1", "sm_name": "Small Cap One Limited", "bm_purpose": "Financial Results",
         "bm_desc": "Quarterly results", "bm_date": "23-Nov-2026"},
        {"bm_symbol": "INFY", "sm_name": "Infosys Limited", "bm_purpose": "Financial Results/Dividend",
         "bm_desc": "Financial results and interim dividend", "bm_date": "25-Nov-2026"},
        {"bm_symbol": "SBIN", "sm_name": "State Bank of India", "bm_purpose": "Fund Raising",
         "bm_desc": "To consider raising of funds via QIP", "bm_date": "26-Nov-2026"},
        {"bm_symbol": "TCS", "sm_name": "Tata Consultancy Services Limited", "bm_purpose": "Other business matters",
         "bm_desc": "General", "bm_date": "26-Nov-2026"},
    ])
    j("nse_corp_actions.json", [
        {"symbol": "ITC", "series": "EQ", "comp": "ITC Limited", "subject": "Interim Dividend - Rs 6.50 Per Share",
         "exDate": "23-Nov-2026", "recDate": "23-Nov-2026"},
        {"symbol": "TATASTEEL", "series": "EQ", "comp": "Tata Steel Limited", "subject": "Bonus 1:1",
         "exDate": "27-Nov-2026", "recDate": "27-Nov-2026"},
        {"symbol": "SMALLCAP2", "series": "EQ", "comp": "Small Cap Two Limited",
         "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
         "exDate": "26-Nov-2026"},
        {"symbol": "HDFCBANK", "series": "EQ", "comp": "HDFC Bank Limited", "subject": "Annual General Meeting",
         "exDate": "26-Nov-2026"},
    ])
    j("bse_results_calendar.json", [
        {"scrip_Code": "500325", "short_name": "RELIANCE", "Long_Name": "Reliance Industries Ltd",
         "meeting_date": "23 Nov 2026"},
        {"scrip_Code": "543210", "short_name": "BSEONLY", "Long_Name": "Bse Only Industries Ltd",
         "meeting_date": "27 Nov 2026"},
    ])
    j("bse_corp_actions.json", [
        {"scrip_code": "500875", "short_name": "ITC", "long_name": "ITC Ltd.", "Ex_date": "23 Nov 2026",
         "Purpose": "Interim Dividend - Rs. - 6.5000"},
    ])
    j("nse_announcements.json", [
        {"symbol": "RELIANCE", "sm_name": "Reliance Industries Limited", "desc": "Financial Result Updates",
         "attchmntText": "Reliance Industries Limited has informed the Exchange about audited financial results for Q2",
         "attchmntFile": "https://nsearchives.nseindia.com/corporate/RELIANCE_23112026.pdf",
         "an_dt": "23-Nov-2026 10:05:12", "sort_date": "2026-11-23 10:05:12", "seq_id": "1001"},
        {"symbol": "INFY", "sm_name": "Infosys Limited", "desc": "Board Meeting Intimation",
         "attchmntText": "Infosys has informed that a meeting of the Board will be held on 25-Nov-2026 to consider results and dividend",
         "attchmntFile": "", "sort_date": "2026-11-23 10:10:00", "seq_id": "1002"},
        {"symbol": "HDFCBANK", "sm_name": "HDFC Bank Limited", "desc": "Outcome of Board Meeting",
         "attchmntText": "The Board has approved and declared an interim dividend of Rs 11 per share. Record date is 2 Dec 2026",
         "attchmntFile": "https://nsearchives.nseindia.com/corporate/HDFCBANK.pdf",
         "sort_date": "2026-11-23 10:12:00", "seq_id": "1003"},
        {"symbol": "SMALLCAP1", "sm_name": "Small Cap One Limited", "desc": "Financial Result Updates",
         "attchmntText": "Unaudited financial results", "sort_date": "2026-11-23 10:14:00", "seq_id": "1004"},
        {"symbol": "ITC", "sm_name": "ITC Limited", "desc": "Updates",
         "attchmntText": "ITC has updated its Dividend Distribution Policy", "sort_date": "2026-11-23 10:15:00",
         "seq_id": "1005"},
        {"symbol": "WIPRO", "sm_name": "Wipro Limited", "desc": "Outcome of Board Meeting",
         "attchmntText": "Outcome of board meeting - approved unaudited standalone and consolidated financial results and sub-division of equity shares",
         "sort_date": "2026-11-23 10:20:00", "seq_id": "1006"},
        {"symbol": "TCS", "sm_name": "Tata Consultancy Services Limited", "desc": "Old news",
         "attchmntText": "Financial results", "sort_date": "2026-11-23 06:00:00", "seq_id": "1000"},
    ])
    j("bse_announcements_p1.json", {"Table": [
        {"NEWSID": "b-1", "SCRIP_CD": 500325, "SLONGNAME": "Reliance Industries Ltd",
         "NEWSSUB": "Reliance Industries Ltd - 500325 - Financial Results For The Quarter Ended Sep 2026",
         "HEADLINE": "Audited financial results for the quarter", "CATEGORYNAME": "Result",
         "SUBCATNAME": "Financial Results", "DT_TM": "2026-11-23T10:06:01.887",
         "ATTACHMENTNAME": "abc123.pdf"},
        {"NEWSID": "b-2", "SCRIP_CD": 500470, "SLONGNAME": "Tata Steel Ltd",
         "NEWSSUB": "Tata Steel Ltd - 500470 - Board Meeting Outcome for Bonus Issue",
         "HEADLINE": "The Board has approved issue of bonus shares in the ratio 1:1", "CATEGORYNAME": "Board Meeting",
         "SUBCATNAME": "Outcome of Board Meeting", "DT_TM": "2026-11-23T10:25:00", "ATTACHMENTNAME": "def456.pdf"},
        {"NEWSID": "b-3", "SCRIP_CD": 543210, "SLONGNAME": "Bse Only Industries Ltd",
         "NEWSSUB": "Bse Only Industries Ltd - 543210 - Results", "HEADLINE": "Unaudited results",
         "CATEGORYNAME": "Result", "SUBCATNAME": "Financial Results", "DT_TM": "2026-11-23T10:26:00"},
    ], "Table1": [{"ROWCNT": 3}]})


FNO = {"RELIANCE": "Reliance Industries Limited", "INFY": "Infosys Limited", "SBIN": "State Bank of India",
       "TCS": "Tata Consultancy Services Limited", "ITC": "ITC Limited", "TATASTEEL": "Tata Steel Limited",
       "HDFCBANK": "HDFC Bank Limited", "WIPRO": "Wipro Limited"}
OTHER = {"SMALLCAP1": "Small Cap One Limited", "SMALLCAP2": "Small Cap Two Limited"}

if __name__ == "__main__":
    write(Path(sys.argv[1]))
