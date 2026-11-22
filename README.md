# FPL bot

Projects every Fantasy Premier League player's expected points for the next six gameweeks, then
plans transfers, the starting XI, bench order, captain and chips for one team. Right now it is a
**dry run**: it logs in, works out exactly what it would change on FPL and emails that, but sends
nothing. Phase 4 turns the sending on.

## How it decides

- **`model.py`**: expected points per player per fixture, built from FPL's scoring rules.
  Minutes come from recent starts and FPL's injury status/news (return dates are parsed).
  Goals and assists use per-90 xG/xA (time-decayed, shrunk toward a price-based prior), scaled
  by opponent and venue. Team attack/defence ratings are fitted to match xG. Clean sheets,
  goals conceded and saves are Poisson, defensive contributions negative binomial (10 actions
  for DEF, 12 for MID/FWD), plus bonus and cards. The scoring constants reproduce FPL's points
  for every player-gameweek of 2025-26 and 2026-27 exactly (`test_model.py`).
- **`optimizer.py`**: one integer program (SciPy/HiGHS) over the horizon. It handles squad
  rules, 3 per club, formations, budget with selling prices, free transfers banked up to 5,
  −4 hits, captaincy, and Wildcard / Free Hit / Bench Boost / Triple Captain. A transfer has to
  beat the value of keeping the free transfer (1.5 points). Transfers come out as like-for-like
  pairs, as FPL requires.
- **`executor.py`**: the hybrid policy.
  - **Automatic:** starting XI, bench order, captain and vice-captain, and transfers that cost
    no points where **each** move adds at least 1 expected point over waiting a week. If the
    last move in a batch doesn't, the plan is re-solved with one fewer.
  - **Asks you first:** anything with a hit, and any chip.
  - If transfers wait, the lineup is picked for the squad you actually have.
- **`bot.py`**: one run. It loads the data, logs in, plans, writes the report and shows the
  exact requests it would send. **`recommend.py`** is the same report without the login.

## How good is it

`python3 backtest.py` replays past gameweeks using only data from before each deadline. In these
replays the model doesn't know about injuries; the live version does.

| | 2025-26 (37 GWs) | 2026-27 GW2–6 |
|---|---|---|
| Correlation with actual points: model / form baseline | 0.578 / 0.502 | 0.559 / 0.466 |
| RMSE: model / form | 1.95 / 2.14 | 2.12 / 2.36 |
| Avg actual points of each week's top 20 picks: model / form | 4.70 / 3.97 | 4.69 / 4.10 |

## Log in once

FPL's login moved to PingOne (account.premierleague.com) this season, and scripted password
logins are refused. Instead, you sign in once in a browser and give the bot the refresh token.
From then on it renews the token itself on every run and keeps it encrypted on the `bot-state`
branch.

1. Open a **private/incognito window** (Chrome or Safari: ⌘⇧N) and sign in at
   fantasy.premierleague.com. A private window matters: your everyday browser would keep
   renewing its own copy of the token and knock the bot's out.
2. Open the console (Chrome: ⌘⌥J. Chrome may ask you to type `allow pasting` first.) Paste
   this, then press Enter:
   ```js
   (() => { for (const s of [localStorage, sessionStorage]) for (const k of Object.keys(s)) if (k.startsWith("oidc.user:")) { copy(JSON.parse(s.getItem(k)).refresh_token); return "Copied the refresh token"; } return "Not found: are you signed in?"; })()
   ```
3. **Close the private window without signing out.** Signing out can cancel the token.
4. GitHub → this repo → Settings → Secrets and variables → Actions → New repository secret:
   - `FPL_REFRESH_TOKEN`: paste.
   - `FPL_STATE_KEY`: any long random string (`openssl rand -hex 32` in Terminal makes one).
     Keep it. Changing it means repeating these steps.
5. Actions → Bot → Run workflow. The email's first line says "Logged in" when it worked.

Don't run the bot with the same token anywhere else, such as on your Mac. Two copies renewing
one token knock each other out. If the bot ever reports that FPL refused the login, repeat
steps 1–4. The new secret takes over on the next run.

## Setup

GitHub: repository variable `FPL_TEAM_ID`. Secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, optional
`EMAIL_TO`, plus the two from "Log in once". Every push to `main` runs the tests and a dry run;
Actions → Bot → Run workflow does the same and emails it.

Locally, read-only:

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 recommend.py --team 1234567        # the report from public data (--email to send it)
python3 -m pytest -q                       # tests
python3 backtest.py                        # model vs. past gameweeks
```

## Limits right now

- Chip values are reported (`CHIPS` table) but the bot doesn't propose or play chips yet.
- Without a login, the squad is the one at the last deadline, and free transfers are counted
  from your history.

## Roadmap

4. **Live.** Send the automatic changes, and email "needs your OK" items with a one-tap
   approval. Chip proposals before they expire (first set: GW19 deadline, Sat 2 Jan). Deadline
   timers via cron-job.org at about 24 h and 90 min before each deadline (GitHub's own schedule
   ran hours late for the ESPN bot). Shadow mode until `BOT_LIVE` is set, a kill switch, no
   writes after the deadline, and every write logged on `bot-state`.

FPL's 2026/27 terms (clause 28(d)) prohibit automated access to the game. This is for one
person's own team, and the risk is theirs to take.
