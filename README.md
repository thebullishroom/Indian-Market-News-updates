# India market bots

Two Discord bots, both free on GitHub Actions:

- **Bot 1, evening data post:** `india_data.py`, workflow `india-evening-data.yml`
- **Bot 2, news bot:** `india_news.py`, workflow `india-news.yml`

## Bot 1: Evening market data post

Every trading day, around 7:15 PM IST, this posts an **Indian Market Wrap** to a Discord channel:

- **FII / DII cash market:** buy, sell and net (₹ crore, provisional), plus a month-to-date total
- **Participant-wise OI:** FII index futures long-short ratio and day-on-day change, net index futures for FII, DII, Pro and Client, and FII index options net
- **NIFTY and BANKNIFTY options** (nearest expiry): highest call and put OI strikes, biggest OI additions, PCR, max pain
- **OI buildup** in stock futures: top 5 in long buildup, short buildup, short covering and long unwinding
- **Delivery:** F&O stocks with unusual delivery spikes, plus the highest delivery % list
- **Advance / decline:** all NSE EQ stocks and F&O stocks

All data comes from free NSE end-of-day files. It runs free on GitHub Actions; nothing to host.

### Setup (about 10 minutes)

1. **Discord webhook:** in your server, create the channel (e.g. `#india-market-data`) → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. **GitHub repo:** create a new repository (private is fine) and upload everything in this folder, including the hidden `.github` folder. On the GitHub website: Add file → Upload files, then drag the folder contents in.
3. **Secret:** repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_INDIA_DATA`
   - Value: the webhook URL
4. **Permissions:** Settings → Actions → General → Workflow permissions → select **Read and write permissions** → Save. (The bot saves its month-to-date totals and delivery history back into the repo.)
5. **Test it:** Actions tab → *India evening data* → Run workflow.
   - Tick **Dry run** first and open the log to see the post without sending it.
   - Then run it again without Dry run to post to Discord.
   - On a weekend, put a recent weekday in **Date** (e.g. `2026-11-20`) and tick **force**.

### How the schedule works

| Run | Time (IST) | What it does |
|---|---|---|
| First | 7:15 PM Mon–Fri | Posts only if all NSE files are out |
| Final | 8:45 PM Mon–Fri | Posts whatever is available if the first run waited |

- It never posts twice for the same day.
- **Market holidays:** if NSE has no bhavcopy for the day, it skips quietly.
- **NSE blocking:** it retries with browser-like headers. If a section can't be fetched, that section says "not available" and the rest still posts. If the main files are blocked, the run is marked failed so GitHub emails you.

### Things to know

- **Month-to-date FII/DII** builds up from the day the bot starts, so in the first month it says "tracked since …". From the next month on, it's a full month total.
- **Delivery spikes** compare today's delivery quantity with each stock's own recent average, so they start showing after 3 trading days of history. Until then you see the highest delivery % list only.
- **BANKNIFTY** only has monthly expiries now, so its options section uses the monthly contract.
- To tweak lists (top 5, minimum turnover, spike threshold), edit the settings near the top of `india_data.py`.

### Run it on your computer (optional)

```bash
pip install -r requirements.txt
python india_data.py --dry-run --final --date 2026-11-20
```


---

## Bot 2: News bot

| When (IST) | What it posts |
|---|---|
| **8:30 AM** trading days | **Morning Brief:** results due today, notable board meetings (fund raising, dividend, bonus, split, buyback), stocks going ex-dividend / ex-bonus / ex-split, and economic events (RBI policy, CPI, WPI, IIP, GDP). It then catches up on any filings made overnight. |
| **Every 15 min, 9:15 AM to 3:30 PM**, plus **7:30 PM** | **Live alerts** when a company files results or announces a dividend, bonus or split, with a link to the filing |
| **Sunday 6 PM** | **Week Ahead:** next week day by day: results, board meetings, ex-dates, economic events and market holidays |

- **Sources:** NSE and BSE corporate filings, both checked every time. If one exchange fails, the other keeps the bot going, and the brief's footer says which one was missing.
- **No repeats:** each filing is only sent once, and the same news filed on both NSE and BSE becomes a single alert.
- **Holidays:** skips the brief and alerts on weekends and NSE holidays. It uses NSE's holiday list, with the 2026 list built in as a backup.
- **F&O stocks only (default):** alerts and name lists cover F&O stocks. Everything else shows up as a count, like "+12 non-F&O companies". For every listed company, change `NEWS_UNIVERSE: fno` to `all` in `india-news.yml` (it gets noisy in results season).
- **Economic calendar** lives in `data/india_economic_calendar.json`. It's filled through March 2027 from RBI's MPC schedule and MoSPI's release calendar. WPI dates follow the usual 14th-of-month pattern. Add new dates there when they're announced (next: RBI's 2027-28 MPC schedule, around March 2027).
- **Very first run:** to avoid flooding the channel, it only alerts on filings from the last hour. After that, it catches everything.

### Setup (5 minutes, same repo)

1. **Discord:** create a channel (e.g. `#india-market-news`) → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.
2. **Secret:** Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_INDIA_NEWS`
   - Value: the webhook URL
3. **Upload** `india_news.py`, the `data` folder, the updated `README.md` and the `tests` folder (Add file → Upload files).
4. **Workflow:** Add file → Create new file → name it exactly `.github/workflows/india-news.yml` (dot at the start, slashes) → paste the contents of `india-news.yml` → Commit.
5. **Test:** Actions → *India news* → Run workflow. Dry run is ticked by default.
   - Mode `weekly` shows next week's preview in the log.
   - Mode `morning` with a weekday date shows that day's brief.
   - Untick Dry run to post for real.

It remembers which filings it has already sent using GitHub's cache, so nothing extra gets committed to the repo. A dry run never marks filings as sent.

### GitHub minutes

There are about 28 short runs per trading day, so roughly 600–650 minutes a month. Bot 1 adds about 45, so you're well inside the free 2,000 for a private repo. If the deals tracker pushes you near the limit, make this repo public (unlimited minutes; secrets stay hidden), or change `*/15` to `*/30` in `india-news.yml`.

---

## Tests

```bash
python tests/test_bot.py     # Bot 1, offline sample files in NSE formats
python tests/test_news.py    # Bot 2, offline sample NSE/BSE responses
```
