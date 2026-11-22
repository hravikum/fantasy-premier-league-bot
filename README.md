# FPL bot

Projects every Fantasy Premier League player's expected points for the next six gameweeks, then
plans transfers, the starting XI, bench order, captain and chips for one team. Phase 1–2 (this
code) is **read-only**: it recommends and emails, and changes nothing on the FPL site.

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
  beat the value of keeping the free transfer (1.5 points).
- **`recommend.py`**: the weekly report: transfers, XI, captain/vice, bench order, the plan for
  the following weeks, each chip's value per week, doubles/blanks, top projections.

## How good is it

`python3 backtest.py` replays past gameweeks using only data from before each deadline. In these
replays the model doesn't know about injuries; the live version does.

| | 2025-26 (37 GWs) | 2026-27 GW2–6 |
|---|---|---|
| Correlation with actual points: model / form baseline | 0.578 / 0.502 | 0.559 / 0.466 |
| RMSE: model / form | 1.95 / 2.14 | 2.12 / 2.36 |
| Avg actual points of each week's top 20 picks: model / form | 4.70 / 3.97 | 4.69 / 4.10 |

Projections are calibrated: players projected 4–5 averaged 4.36 and those projected 6–8
averaged 7.05. The archive's own "xP" column was recorded after the matches, so it isn't used
as a benchmark.

## Setup

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 recommend.py                       # projections only
python3 recommend.py --team 1234567        # your recommendations (team id is in the Points page URL)
python3 recommend.py --team 1234567 --email
python3 -m pytest -q                       # tests
```

`.env` (never commit it): `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (myaccount.google.com/apppasswords),
optional `EMAIL_TO`.

On GitHub: repository variable `FPL_TEAM_ID`, secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`,
optional `EMAIL_TO`. Then Actions → Recommend → Run workflow.

## Limits right now

- Public FPL data shows your squad as of the **last** deadline. Transfers you've made since
  then won't be seen until phase 3 (login).
- Free transfers are counted from your history. If FPL hands out extra ones (as for AFCON in
  2025-26), pass `--ft N`.
- Chip values are reported, not acted on.

## Roadmap

3. **Login + dry-run executor.** Sign in once in a separate browser profile and capture the
   refresh token. The bot rotates it on every run (FPL now uses PingOne OAuth;
   `users.premierleague.com` is gone). Reads `my-team` for exact selling prices and free
   transfers, then shows the exact payloads it would send.
4. **Live, hybrid.** It sets the XI, captain/vice and free transfers by itself. Anything with a
   hit or a chip is emailed for approval first. Deadline timers via cron-job.org (GitHub's own
   schedule ran hours late for the ESPN bot), shadow mode until `BOT_LIVE` is set, a kill
   switch, no writes after the deadline, and every write logged.

FPL's 2026/27 terms (clause 28(d)) prohibit automated access to the game. This is for one
person's own team, and the risk is theirs to take.
