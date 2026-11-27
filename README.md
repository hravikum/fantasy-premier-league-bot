# FPL bot

Runs one Fantasy Premier League team. Before every deadline it projects every player's expected
points for the coming weeks, plans transfers, lineup, captain and chips, and makes the changes.
Hits and chips wait for your OK. Everything is posted to a Slack channel once Slack is set up, or
emailed (formatted HTML, with a plain-text copy) until then, and anything that fails sends an
**ACTION NEEDED** message by both Slack and email so you can step in.

## Each gameweek

cron-job.org starts a short "tick" on GitHub every 30 minutes (`timers.py`). Each tick looks at
FPL's next deadline and runs whichever job is due (`schedule.py`), once per gameweek:

| When | Job | What happens |
|---|---|---|
| ~24 h before the deadline | **preview** | Emails the plan. Each hit or chip it wants is a numbered decision with its own **Yes / No** buttons (or reply "1 yes", "2 no"). Writes the watchlist for the news check. Changes nothing. |
| a few hours before | **Claude news check** | A scheduled Claude task reads press conferences and predicted lineups for the watchlist players, and notes start chances that FPL's flags don't show yet. It runs every 6 hours and works only near a deadline. |
| ~2 h before | **main run** | Logs in, applies the news and your reply, makes the changes, reads them back from FPL, and emails what it did. |
| ~45 min before | **final check** | Re-checks injury news and fixes the lineup and captain if needed. If the main run failed, it runs it instead. Emails only if it changed or failed something. **If you changed your team on FPL after the main run** (captain, bench, a transfer), it leaves it as you set it and only warns you (ACTION NEEDED) if a starter's chance of playing has dropped since the main run. |

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
  - **Ruled-out players go now.** A squad player FPL rules out (0%: injured, suspended, left the
    club) for this gameweek and the next is sold this week with a free transfer, without having to
    clear the bar. That frees his squad spot before his price drops. Exception: if the best plan
    still has him (kept, or bought back) at the end of the 6 weeks, he's worth keeping and stays.
    No free transfer left means he waits; a hit for him would need your yes like any other.
  - **Soft club limit.** FPL allows 3 players per club. The bot adds a third from one club only if
    he adds 2+ expected points over 6 gameweeks compared with the best alternative (the plan pays
    1/3 point a week for each club it has 3 from). This spreads the risk of one club having a bad
    week at little cost. The email says when a move ends a triple-up, or why the plan keeps one.
    `python explain.py --squad --team <id>` (or the explain job with player `--squad=3`) shows
    ownership, how our projections compare with FPL's and the crowd's, club exposure, the defences
    you're backing, and what stricter limits would cost.
- **Fixtures table**: every plan and preview email lists your squad, this week's buys and the
  plan's buys for the next two weeks, with each one's next 3 opponents and expected points. The
  opponent is green for an easier fixture than average for that player and red for a harder one
  (his team's attack for MID/FWD, the opponent's attack for GK/DEF). "out" marks a gameweek he's
  ruled out.
- **Chips (`chips.py`)**: each remaining chip's value in every week up to its expiry (first set:
  GW19 deadline, Sat 2 Jan), spread one chip a week for the most points.
  - A chip is proposed when its best week is the coming one and it's clearly worth it:
    Triple Captain 7+, Bench Boost 15+, Free Hit 10+, Wildcard 12+ expected points.
  - Near expiry it's use it or lose it.
- **Hybrid policy (`executor.py`)**:
  - Automatic: lineup, bench order, captain and vice-captain, and free transfers that clear the
    bar.
  - Asks first: hits and chips, each as its own decision.
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
- **Saved state**: each run retries fetching `bot-state` before giving up. If GitHub still doesn't
  answer, the run stops before doing anything and emails you; the next tick tries again. It never
  mistakes a GitHub hiccup for a first run (that caused a false "could not save its login" on Nov 23).
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
  optionally `EMAIL_TO`, `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` (Slack, below).
- Optional variables for Slack: `SLACK_USER_ID`, `EMAIL_TOO`.

**Timers (once, on the Mac):**

```
git clone https://github.com/hravikum/fantasy-premier-league-bot ~/Projects/fantasy-premier-league-bot
cd ~/Projects/fantasy-premier-league-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# .env:  CRONJOB_API_KEY=... (same as the ESPN bot)   GITHUB_DISPATCH_TOKEN=... (fine-grained,
#        this repository only, Actions: read and write)
.venv/bin/python3 timers.py --dry-run && .venv/bin/python3 timers.py
```

**Decisions (`approvals.py`).** Each hit or chip is a separate decision.
- **Buttons:** tap Yes or No. That starts an email to the bot, and you press Send.
- **Typed replies:** reply to the preview with "1 yes" and "2 no" (or "yes to 1, no to 2", or just
  "yes, no" in the order listed).
- **No answer:** an unanswered decision counts as no.
- **Changing your mind:** a later answer replaces an earlier one, until the main run.
- **A bare "yes" with several decisions** is unclear and approves nothing; the main-run email says
  so.

The main run reads your answers over IMAP with the same Gmail app password. If a main-run email
says it couldn't read them, turn on IMAP in Gmail: Settings → Forwarding and POP/IMAP.

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

**Slack.** With the secrets `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` set, every message goes to a Slack
channel instead of email (`mailer.py`, `slack.py`, `doc.to_slack`):
- The message in the channel has the warnings, your decisions and what the bot changes; the
  report goes in its thread: transfers and the fixtures table in one reply, then the XI, plan,
  chips and the top projections table in another (Slack takes one table a message, at its bottom).
- The other tables become lines that read on a phone, and fixture colours become 🟢 easier,
  ⚪ average and 🔴 harder, as Slack can't colour text.
- **ACTION NEEDED** messages and failure alerts always go by email too, and @-mention you in Slack
  when the variable `SLACK_USER_ID` is set.
- If a Slack post fails, that message is emailed instead. If Slack refuses a layout, the message is
  posted as plain text.
- While the variable `EMAIL_TOO` is `true`, everything is emailed as well (the trial).
- **Decisions** (a hit or a chip) have real Yes and No buttons in Slack. A tap writes your answer
  under them ("✅ You said yes, Sat 2:02 PM ET"); tap the other one to change it. The email's
  buttons and replies still work too: for each decision your latest answer counts, wherever you
  gave it. The main run reads your answers, then swaps the buttons for "🔒 Answers closed".
  Taps go to a small Cloudflare Worker (`slack-worker/worker.js`, free plan), which checks they
  came from Slack and from you and writes the answer into the message; it keeps nothing itself.

Setting it up, once (same workspace as the ESPN bot, its own app and channel):
1. In the workspace, create a channel such as `#fantasy-premier-league`. On your phone, set its
   notifications to every new message.
2. api.slack.com/apps → Create New App → From a manifest: pick the workspace, paste
   `slack-app-manifest.yml`, create, then Install to workspace. This is a new app ("Fantasy Premier League Bot"), not
   the ESPN bot's "Fantasy Bot".
3. OAuth & Permissions shows the Bot User OAuth Token (`xoxb-…`): the secret `SLACK_BOT_TOKEN`. The
   channel's id is at the bottom of its About tab (click the channel's name): the secret
   `SLACK_CHANNEL`. Your member ID (your profile, ⋮, Copy member ID): the variable `SLACK_USER_ID`.
4. In the channel, type `/invite @Fantasy Premier League Bot`.
5. Set the variable `EMAIL_TOO` to `true`, then Actions → Bot → Run workflow → `dry-run`: the plan
   should appear in the channel, with the report in its thread, and in your inbox.

The Yes/No buttons, once (about 15 minutes):
1. Cloudflare, the account the ESPN bot's Worker uses: Workers & Pages → Create → Create Worker,
   name it e.g. `fpl-slack-worker`, Deploy. Then Edit code, replace everything with
   `slack-worker/worker.js`, and Deploy again.
2. The Worker's Settings → Variables and Secrets, three secrets: `SLACK_SIGNING_SECRET` (the Slack
   app's Basic Information page, App Credentials, Signing Secret), `SLACK_BOT_TOKEN` (the same
   `xoxb-` token as the repository secret) and `SLACK_USER_ID` (your member ID). Copy the Worker's
   address (`https://….workers.dev`).
3. The Slack app (api.slack.com/apps): Interactivity & Shortcuts → On, paste the Worker's address
   as the Request URL, Save. OAuth & Permissions → Bot Token Scopes → Add `channels:history`, then
   reinstall the app when Slack asks. If the `xoxb-` token changed, update `SLACK_BOT_TOKEN` in the
   repository and in the Worker.
4. Actions → Bot → Run workflow → `slack-check`: a test decision appears in the channel. Tap Yes or
   No: your answer should show under the buttons within a second or two. Run `slack-check` again: it
   reads your answer back the way the main run will, says what it read in the test's thread, and
   closes the test.

To change the Worker later, paste the new `worker.js` the same way. Its tests: `node slack-worker/worker.test.js`.

After a gameweek in which every email had its Slack twin, delete `EMAIL_TOO`. To go back to email
only, delete the two Slack secrets.

**Run by hand:** Actions → Bot → Run workflow.

| Job | What it does |
|---|---|
| `dry-run` | The report, sent (Slack or email) |
| `slack-check` | Tests the Slack Yes/No buttons: run, tap, run again (Slack, above) |
| `preview`, `main`, `final` | Runs that job now; respects `BOT_LIVE`; doesn't touch the schedule |
| `check-write` | The no-op write check |
| `explain` (with a player name) | Why the model rates him as it does, and what he'd add to your team |

**Locally (read-only):**

```
python3 recommend.py --team 1234567        # the report from public data
python3 explain.py Haaland --team 1234567  # why the model rates a player as it does, and his value to your team
python3 -m pytest -q                       # 85 tests, including preview → main → final against a fake FPL
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
