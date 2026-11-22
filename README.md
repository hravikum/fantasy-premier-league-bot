# FPL bot

Runs one Fantasy Premier League team. Before every deadline it projects every player's expected
points for the coming weeks, plans transfers, lineup, captain and chips, and makes the changes.
Hits and chips wait for your OK. Everything is emailed (formatted HTML, with a plain-text copy),
and anything that fails sends an **ACTION NEEDED** email so you can step in.

## Each gameweek

cron-job.org starts a short "tick" on GitHub every 30 minutes (`timers.py`). Each tick looks at
FPL's next deadline and runs whichever job is due (`schedule.py`), once per gameweek:

| When | Job | What happens |
|---|---|---|
| ~24 h before the deadline | **preview** | Emails the plan. Anything needing your OK (a hit, a chip) is listed: **reply YES** to approve it all, NO or no reply to skip. Writes the watchlist for the news check. Changes nothing. |
| a few hours before | **Claude news check** | A scheduled Claude task reads press conferences and predicted lineups for the watchlist players, and notes start chances that FPL's flags don't show yet. It runs every 6 hours and works only near a deadline. |
| ~2 h before | **main run** | Logs in, applies the news and your reply, makes the changes, reads them back from FPL, and emails what it did. |
| ~45 min before | **final check** | Re-checks injury news and fixes the lineup and captain if needed. If the main run failed, it runs it instead. Emails only if it changed or failed something. |

A failed main run is retried at the next tick, and the final check is a last retry.

## How it decides

- **Expected points (`model.py`)**, built from FPL's scoring rules:
  - Minutes: recent starts, FPL's injury flags (return dates parsed) and the news check.
  - Goals and assists: per-90 non-penalty xG and xA (time-decayed, shrunk toward a price-based
    prior), scaled by the opponent and venue.
  - Penalties: they go to the first taker on FPL's set-piece list likely to be on the pitch, so a
    stand-in inherits them.
  - Clean sheets, goals conceded and saves come from Poisson team ratings fitted on xG.
  - Defensive contributions, bonus and cards are modelled too.
  - The scoring constants reproduce FPL's points exactly for every player-gameweek of 2025-26 and
    2026-27.
- **Plan (`optimizer.py`, `recommend.py`)**: one integer program over 6 gameweeks. It covers squad
  rules, budget with selling prices, free transfers banked up to 5, −4 hits and captaincy.
  - Each transfer made this week has to add at least 1 expected point over waiting a week.
  - If the batch's last transfer doesn't, it's re-planned with one fewer.
- **Chips (`chips.py`)**: each remaining chip's value in every week up to its expiry (first set:
  GW19 deadline, Sat 2 Jan), spread one chip a week for the most points.
  - A chip is proposed when its best week is the coming one and it's clearly worth it:
    Triple Captain 7+, Bench Boost 15+, Free Hit 10+, Wildcard 12+ expected points.
  - Near expiry it's use it or lose it.
- **Hybrid policy (`executor.py`)**:
  - Automatic: lineup, bench order, captain and vice-captain, and free transfers that clear the
    bar.
  - Asks first: hits and chips.
  - A chip you play yourself on the site is kept. With your Wildcard or Free Hit active, the bot
    leaves transfers and lineup to you.

## Safety

- **Shadow mode**: nothing is sent to FPL until the repository variable `BOT_LIVE` is `true`.
  Shadow runs do everything else and email it marked [shadow]. Setting `BOT_LIVE` to anything
  else is the kill switch.
- **Deadline guard**: no request within 2 minutes of the deadline.
- **Checks**: every change is read back from FPL and checked. Every request is logged in
  `state/writes.log` on the `bot-state` branch.
- **Failure emails**: a refused request, a crash, a failed login or a lost login token sends an
  ACTION NEEDED email with what to do by hand. GitHub also emails you when a run fails, and
  cron-job.org emails you if it can't start one.
- **`check-write` job**: re-saves your lineup unchanged and reads it back, which proves FPL
  accepts the bot's requests. It last passed Nov 22 (HTTP 202, lineup unchanged).

## How good the projections are

`python3 backtest.py` replays past gameweeks using only data from before each deadline. The
replays have no injury news, which the live bot does have.

| | 2025-26 (37 GWs) | 2026-27 GW2–6 |
|---|---|---|
| Correlation with actual points: model / form baseline | 0.578 / 0.502 | 0.559 / 0.466 |
| Avg actual points of each week's top 20 picks: model / form | 4.70 / 3.97 | 4.69 / 4.10 |

## Setup

GitHub repository settings:
- Variables: `FPL_TEAM_ID`, and `BOT_LIVE` (`true` to send changes).
- Secrets: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `FPL_REFRESH_TOKEN`, `FPL_STATE_KEY`, and
  optionally `EMAIL_TO`.

**Timers (once, on the Mac):**

```
git clone https://github.com/hravikum/fantasy-premier-league-bot ~/Projects/fantasy-premier-league-bot
cd ~/Projects/fantasy-premier-league-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# .env:  CRONJOB_API_KEY=... (same as the ESPN bot)   GITHUB_DISPATCH_TOKEN=... (fine-grained,
#        this repository only, Actions: read and write)
.venv/bin/python3 timers.py --dry-run && .venv/bin/python3 timers.py
```

**Approvals by reply** need IMAP access to Gmail with the same app password. If a main-run
email says it couldn't read your reply, turn on IMAP in Gmail: Settings → Forwarding and
POP/IMAP.

**Log in once.** FPL's login is PingOne OAuth, and scripted password logins are refused:
1. Sign in at fantasy.premierleague.com in a **private window**.
2. In the console (⌘⌥J; Chrome may ask you to type `allow pasting` first), run:
   ```js
   (() => { for (const s of [localStorage, sessionStorage]) for (const k of Object.keys(s)) if (k.startsWith("oidc.user:")) { copy(JSON.parse(s.getItem(k)).refresh_token); return "Copied the refresh token"; } return "Not found: are you signed in?"; })()
   ```
3. Close the window **without signing out**.
4. Save the token as `FPL_REFRESH_TOKEN`. `FPL_STATE_KEY` can be any long random string; keep it.

The bot renews the token on every run and keeps it encrypted on `bot-state`. Don't use the same
token anywhere else, such as on your Mac. If an email says FPL refused the login, repeat these
steps; the new secret takes over on the next run.

**Run by hand:** Actions → Bot → Run workflow.

| Job | What it does |
|---|---|
| `dry-run` | The report, emailed |
| `preview`, `main`, `final` | Runs that job now; respects `BOT_LIVE`; doesn't touch the schedule |
| `check-write` | The no-op write check |
| `explain` (with a player name) | Why the model rates him as it does, and what he'd add to your team |

**Locally (read-only):**

```
python3 recommend.py --team 1234567        # the report from public data
python3 explain.py Haaland --team 1234567  # why the model rates a player as it does, and his value to your team
python3 -m pytest -q                       # 42 tests, including preview → main → final against a fake FPL
python3 backtest.py                        # projections vs. past gameweeks
```

## Not done yet

- **Corners and free kicks.** FPL's match stats can't separate set-piece xA or xG, so only
  penalties use FPL's set-piece order.
- **The start-chance ceiling** for unflagged regulars (92%, keepers 96%) is a reasoned guess.
  `state/flags.csv` records FPL's flags at each deadline so it can be measured after a few
  gameweeks.

FPL's 2026/27 terms (clause 28(d)) prohibit automated access to the game. This is for one
person's own team, and the risk is theirs to take.
