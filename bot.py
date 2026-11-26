"""One run of the bot.

python3 bot.py                     dry run: the report and what it would change (sends nothing)
python3 bot.py --email             the same, emailed
python3 bot.py --job tick          what cron-job.org starts every 30 minutes: runs the job that's due
python3 bot.py --job preview|main|final    one of them now (doesn't touch the schedule)
python3 bot.py --job check-write   re-save the current lineup unchanged: proves FPL accepts the bot's writes

Jobs (schedule.py has the timing):
  preview  ~24 h before the deadline: the plan, chip proposals, and one YES/NO question for anything
           that needs your OK. Writes the watchlist for the Claude news check. Changes nothing.
  main     ~2 h before: logs in, reads the news check and your reply, makes the changes, reads them
           back from FPL and emails what it did. If anything fails: an ACTION NEEDED email.
  final    ~45 min before: if the main run didn't finish, runs it now; otherwise re-checks injuries
           and fixes the lineup and captain if needed. Emails only if it changed or failed something.
Changes are only sent when the repository variable BOT_LIVE is "true". Until then every job runs
as a shadow: the same emails, marked [shadow], and nothing is sent.
"""
import argparse
import csv
import json
import os
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from dotenv import load_dotenv

import approvals
import auth
import chips
import data
import doc
import executor
import model
import news
import optimizer
import recommend
import schedule
import team as team_module

EASTERN = "America/New_York"
FLAGS = Path(__file__).with_name("state") / "flags.csv"
LINEUP_CHIPS = {"3xc", "bboost"}          # played through the lineup request
TRANSFER_CHIPS = {"wildcard", "freehit"}  # played through the transfers request


@dataclass
class Context:
    season: data.Season
    now: pd.Timestamp
    gw: int
    deadline: pd.Timestamp
    horizon_gws: list
    all_gws: list
    per_gw: pd.DataFrame
    pf: pd.DataFrame
    team: team_module.Team
    token: str | None
    login: str
    news_lines: list
    live: bool
    started: pd.Timestamp            # the real time the run started; now + elapsed = the time of a write

    def clock(self):
        return self.now + (pd.Timestamp.now(tz="UTC") - self.started)

    @property
    def players(self):
        return self.season.players

    @property
    def plan_xp(self):
        return self.per_gw[self.horizon_gws]

    @property
    def local_deadline(self):
        return self.deadline.tz_convert(EASTERN).strftime("%a %d %b %H:%M")


def load_context(now, live, need_login=False):
    started = pd.Timestamp.now(tz="UTC")
    season = data.load_live()
    upcoming = season.events[season.events.deadline_time > now]
    if upcoming.empty:
        return None
    gw, deadline = int(upcoming.id.min()), upcoming.deadline_time.min()
    history = data.load_with_prior(season)
    token = None
    try:
        token = auth.access_token()
        my_team = team_module.load_private(token, season.players)
        login = "Logged in: squad, selling prices and free transfers are live from FPL."
    except auth.LoginNeeded as problem:
        if need_login:
            raise
        team_id = os.getenv("FPL_TEAM_ID")
        if not team_id:
            raise SystemExit(f"Not logged in ({problem}) and FPL_TEAM_ID isn't set either.")
        my_team = team_module.load(int(team_id), season.players)
        login = f"NOT LOGGED IN: {problem}\nUsing public data, which shows the squad as of the last deadline."
    horizon_gws, all_gws = recommend.gameweeks(gw, my_team)
    overrides, news_lines = news.load(gw, now)
    per_gw, pf = model.project(history, season.players, season.fixtures, season.teams, all_gws, now,
                               model.Params(start_ceiling=model.LIVE_START_CEILING), start_news=overrides)
    return Context(season, now, gw, deadline, horizon_gws, all_gws, per_gw, pf, my_team, token, login,
                   news_lines, live, started)


def email(ctx_or_live, subject, message):
    """Send a doc.Message: to Slack once it's set up, and by email as HTML with a plain-text copy
    (mailer.py says when). Shadow runs are marked in the subject; an alert always goes both ways."""
    import mailer
    live = ctx_or_live.live if isinstance(ctx_or_live, Context) else ctx_or_live
    mailer.send(("" if live else "[shadow] ") + subject, doc.to_text(message), doc.to_html(message),
                slack_posts=doc.to_slack(message), urgent=message.alert)


def pills(ctx):
    return [("Live: changes are sent" if ctx.live else "Shadow: nothing is sent", "good" if ctx.live else "info"),
            ("Logged in" if ctx.token else "Not logged in", None if ctx.token else "bad")]


def context_blocks(ctx):
    """Login trouble and the team news, at the top of every email."""
    blocks = []
    if not ctx.token:
        blocks.append(doc.Callout("Not logged in", ctx.login.splitlines(), "bad"))
    if ctx.news_lines:
        warn = any(line.startswith(("WARNING", "No Claude news check")) for line in ctx.news_lines)
        blocks.append(doc.Callout("Team news", [line.strip() for line in ctx.news_lines], "warn" if warn else "info"))
    return blocks


def message(ctx, title, blocks, alert=False):
    return doc.Message(title, f"Deadline {ctx.local_deadline} ET · {ctx.team.name} · {recommend.money(ctx.team)}",
                       pills(ctx), blocks,
                       footer=f"Run: {run_link()}", alert=alert)


def run_link():
    if os.getenv("GITHUB_RUN_ID"):
        return f"{os.getenv('GITHUB_SERVER_URL')}/{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.getenv('GITHUB_RUN_ID')}"
    return "(local run)"


def log_flags(ctx, job):
    """FPL's injury flags at each deadline run: in a few weeks this measures how often unflagged
    regulars actually start (the start ceiling in model.py)."""
    flagged = ctx.players[ctx.players.status != "a"]
    new = not FLAGS.exists()
    FLAGS.parent.mkdir(exist_ok=True)
    with FLAGS.open("a", newline="") as f:
        out = csv.writer(f)
        if new:
            out.writerow(["at", "gw", "job", "element", "status", "chance", "news"])
        for p in flagged.itertuples():
            out.writerow([ctx.now.isoformat(), ctx.gw, job, p.id, p.status, p.chance, p.news])


# ---------------------------------------------------------------- jobs

def write_watchlist(ctx, plan):
    """The players the Claude news check looks at: the squad plus the plan's buys this week and next."""
    later = [i for w in plan.weeks[:2] for i in w.transfers_in]
    news.write_watchlist(ctx.gw, ctx.deadline, list(ctx.team.squad) + later, ctx.players, ctx.season.teams)


def dry_run(ctx, send_email):
    analysis = recommend.analyse(ctx.per_gw, ctx.players, ctx.team, ctx.horizon_gws)
    write_watchlist(ctx, analysis.base)       # so the news check follows the current plan
    actions = executor.decide(ctx.team, analysis.base.weeks[0], analysis.wait.weeks[0], analysis.gain, ctx.players,
                              sold=analysis.sold)
    blocks = context_blocks(ctx) + executor.describe(actions, ctx.players, "What the bot would change on FPL") + \
        recommend.report(analysis, ctx.per_gw, ctx.pf, ctx.players, ctx.season.fixtures, ctx.season.teams,
                         ctx.team, ctx.horizon_gws)
    msg = message(ctx, f"GW{ctx.gw} plan", blocks)
    print(doc.to_text(msg))
    if send_email:
        email(True, f"GW{ctx.gw} plan (deadline {ctx.local_deadline})", msg)
    return True


def preview(ctx):
    a = recommend.analyse(ctx.per_gw, ctx.players, ctx.team, ctx.horizon_gws)
    first = a.base.weeks[0]
    name = ctx.players.set_index("id")["web_name"]
    decisions = []
    if first.hits:
        moves = ", ".join(f"{name[o]} → {name[i]}" for o, i in zip(first.transfers_out, first.transfers_in))
        decisions.append({"kind": "transfers", "in": first.transfers_in, "out": first.transfers_out, "hits": first.hits,
                          "text": f"Take {first.hits} hit{'s' if first.hits > 1 else ''} (-{4 * first.hits}) "
                                  f"for these transfers: {moves}"})
    if a.chip_proposal:
        value = a.wildcard_gain if a.chip_proposal == "wildcard" else a.chip_table.loc[ctx.gw, a.chip_proposal]
        decisions.append({"kind": "chip", "chip": a.chip_proposal,
                          "text": f"Play {chips.NAMES[a.chip_proposal]} this week (about {value:+.1f} expected points)"})
    for n, decision in enumerate(decisions, start=1):
        decision["n"] = n
    # What the main run does if you say no to everything: no hits, no chip.
    if first.hits:
        base, wait, gain, _, sold, _ = recommend.plan_this_week(ctx.plan_xp, ctx.players, ctx.team, ctx.gw,
                                                                optimizer.Settings(max_hits=0))
        default = executor.decide(ctx.team, base.weeks[0], wait.weeks[0], gain, ctx.players, sold=sold)
    else:
        default = executor.decide(ctx.team, first, a.wait.weeks[0], a.gain, ctx.players, sold=a.sold)
    code = None
    if decisions:
        code = approvals.new_code()
        approvals.save({"gw": ctx.gw, "code": code, "created": ctx.now.isoformat(), "decisions": decisions})
    write_watchlist(ctx, a.base)

    blocks = context_blocks(ctx)
    if decisions:
        lines = []
        for d in decisions:
            lines.append([doc.Tone(f"{d['n']}. ", bold=True), d["text"]])
            lines.append([doc.Button("Yes", approvals.button_link(code, d["n"], "yes"), "good",
                                     plain=f"   reply \"{d['n']} yes\" or \"{d['n']} no\""),
                          doc.Button("No", approvals.button_link(code, d["n"], "no"), "bad")])
        lines.append(doc.Tone("Answer each one: tap Yes or No (it starts an email to the bot; press Send), or reply "
                              "with \"1 yes\", \"2 no\". Anything you don't answer is skipped, and a later answer "
                              "replaces an earlier one, until the main run about 2 hours before the deadline.",
                              "muted"))
        blocks.append(doc.Callout(f"{len(decisions)} decision{'s' if len(decisions) > 1 else ''} for you", lines, "warn"))
    blocks += executor.describe(default, ctx.players, "At the main run (about 2 hours before the deadline)"
                                + ("" if not decisions else ", if you say no" if len(decisions) == 1
                                   else ", if you say no to everything"))
    blocks += recommend.report(a, ctx.per_gw, ctx.pf, ctx.players, ctx.season.fixtures, ctx.season.teams,
                               ctx.team, ctx.horizon_gws)
    msg = message(ctx, f"GW{ctx.gw} preview", blocks)
    print(doc.to_text(msg))
    subject = f"GW{ctx.gw} preview, deadline {ctx.local_deadline}"
    if decisions:
        subject += f": {len(decisions)} decision{'s' if len(decisions) > 1 else ''} for you [{code}]"
    email(ctx, subject, msg)
    return True


def approved_actions(ctx, approved, notes):
    """The actions for this week given the decisions you said yes to ({kind: decision}), or
    (None, None) to fall back to the no-approval plan."""
    if not approved:
        return None, None
    team, players, gw = ctx.team, ctx.players, ctx.gw
    available, _ = team.chips_available(gw)
    chip = approved["chip"]["chip"] if "chip" in approved and approved["chip"]["chip"] in available else None
    settings = optimizer.Settings(max_hits=0, triple_captain_gw=gw if chip == "3xc" else None,
                                  bench_boost_gw=gw if chip == "bboost" else None)
    args = (ctx.plan_xp, players, team.squad, team.bank, team.free_transfers, team.sell_prices)
    if chip in TRANSFER_CHIPS:
        if "transfers" in approved:
            notes.append(f"The {chips.NAMES[chip]} replaces the transfers you approved: it re-picks the squad.")
        xp = ctx.per_gw[[gw]] if chip == "freehit" else ctx.plan_xp
        plan = optimizer.plan(xp, *args[1:], optimizer.Settings(wildcard_gw=gw, ft_value=0 if chip == "freehit" else 1.5))
        week = plan.weeks[0]
        actions = []
        if week.transfers_in:
            actions.append(executor.Action("transfers", "approved", f"{chips.NAMES[chip]} (you approved)", "transfers/",
                                           executor.transfers_payload(team.entry, week, players, team.sell_prices, chip)))
        actions.append(executor.lineup_action(team, week, players))
        return actions, chip
    wanted = approved.get("transfers")
    if wanted:
        ins, outs = wanted["in"], wanted["out"]
        by_id = players.set_index("id")
        ready = all(i in by_id.index and (by_id.loc[i, "status"] == "a" or by_id.loc[i, "chance"] >= 75) for i in ins)
        if set(outs) <= set(team.squad) and not set(ins) & set(team.squad) and ready:
            forced = optimizer.plan(*args, replace(settings, max_hits=2, forced={gw: (ins, outs)}))
            free = recommend.plan_this_week(ctx.plan_xp, players, team, gw, settings)[0]
            if forced.weeks and forced.objective >= free.objective:
                week = forced.weeks[0]
                return [executor.Action("transfers", "approved", f"{week.hits} hit(s) (you approved)", "transfers/",
                                        executor.transfers_payload(team.entry, week, players, team.sell_prices)),
                        executor.lineup_action(team, week, players, chip)], chip
            notes.append("The hit transfers you approved no longer beat the free-transfer plan on today's data, "
                         "so they weren't made.")
        else:
            notes.append("The hit transfers you approved can't be made as approved (a player is now doubtful or "
                         "already moved), so they weren't made.")
    if chip:
        base, wait, gain, _, sold, _ = recommend.plan_this_week(ctx.plan_xp, players, team, gw, settings)
        return executor.decide(team, base.weeks[0], wait.weeks[0], gain, players, chip=chip, sold=sold), chip
    return None, None


def main_run(ctx, job="main"):
    """The changes for the coming deadline. Returns (ok, chip played)."""
    log_flags(ctx, job)
    notes, proposal = [], approvals.load(ctx.gw)
    decisions = proposal["decisions"] if proposal else []
    answers = {}
    if decisions:
        try:
            answers, unclear = approvals.check(proposal["code"], pd.Timestamp(proposal["created"]), len(decisions))
            notes += unclear
        except Exception as problem:
            notes.append(f"Couldn't read your answers ({type(problem).__name__}: {problem}), so every decision "
                         "was treated as no.")
    said = {"yes": "you said yes", "no": "you said no", None: "no answer, so no"}
    for d in decisions:
        notes.append(f"{d['n']}. {d['text']}: {said[answers.get(d['n'])]}.")
    approved = {d["kind"]: d for d in decisions if answers.get(d["n"]) == "yes"}
    active = ctx.team.active_chip
    if active:
        notes.append(f"You've already played {chips.NAMES.get(active, active)} this gameweek; the bot keeps it.")
        if approved.pop("chip", None):
            notes.append("So the chip you approved wasn't played: one chip a gameweek.")
    actions, chip = approved_actions(ctx, approved, notes)
    if actions is None and active in TRANSFER_CHIPS:
        notes.append("With that chip active, transfers and the lineup are left to you.")
        actions, chip = [], active
    elif actions is None:
        settings = optimizer.Settings(max_hits=0, triple_captain_gw=ctx.gw if active == "3xc" else None,
                                      bench_boost_gw=ctx.gw if active == "bboost" else None)
        base, wait, gain, _, sold, _ = recommend.plan_this_week(ctx.plan_xp, ctx.players, ctx.team, ctx.gw, settings)
        actions = executor.decide(ctx.team, base.weeks[0], wait.weeks[0], gain, ctx.players,
                                  chip=active, chip_already_set=bool(active), sold=sold)
        chip = active
    ok = executor.send(actions, ctx.token, ctx.deadline, ctx.live, job, now=ctx.clock())
    problems = []
    if ctx.live and any(a.outcome == "sent" for a in actions):
        try:
            problems = executor.verify(actions, data.get(f"my-team/{ctx.team.entry}/", token=ctx.token))
        except Exception as problem:
            problems = [f"couldn't read my-team back to check ({type(problem).__name__}: {problem})"]
    success = ok and not problems
    lineup = next((a for a in actions if a.kind == "lineup"), None)
    blocks = []
    if not success:
        failed = next((a.outcome for a in actions if a.outcome.startswith(("FAILED", "not sent"))), "; ".join(problems))
        later = ("It tries again at the next check and at the final check (about 45 min before the deadline)."
                 if job == "main" else "This was the last scheduled try before the deadline.")
        blocks.append(doc.Callout("Action needed", [failed, later, doc.Tone(
            f"To be safe, make the changes below yourself on the FPL site before {ctx.local_deadline} ET.", bold=True)],
            "bad"))
    blocks += context_blocks(ctx)
    if notes:
        blocks.append(doc.Callout("Your decisions and chips", notes, "info"))
    blocks += executor.describe(actions, ctx.players, "Changes")
    if problems:
        blocks.append(doc.Callout("Checked on FPL afterwards", problems, "bad"))
    if lineup:
        blocks += lineup_section(ctx, lineup.payload)
    changed = [a for a in actions if a.mode in ("auto", "approved")]
    summary = " and ".join({"transfers": "transfers", "lineup": "lineup"}[a.kind] for a in changed) or "no changes needed"
    title = f"GW{ctx.gw} {job} run: {'done' if success else 'ACTION NEEDED'}"
    msg = message(ctx, title, blocks, alert=not success)
    print(doc.to_text(msg))
    if success:
        email(ctx, f"GW{ctx.gw} {job} run done: {summary}", msg)
    else:
        email(True, f"ACTION NEEDED: GW{ctx.gw} {job} run couldn't make its changes", msg)
    return success, chip


def lineup_section(ctx, payload):
    """The lineup a request sets, as email blocks."""
    picks = sorted(payload["picks"], key=lambda p: p["position"])
    week = SimpleNamespace(gw=ctx.gw, xi=[p["element"] for p in picks if p["position"] <= 11],
                           bench=[p["element"] for p in picks if p["position"] > 11],
                           captain=next(p["element"] for p in picks if p["is_captain"]),
                           vice_captain=next(p["element"] for p in picks if p["is_vice_captain"]))
    xp = sum(ctx.per_gw.loc[i, ctx.gw] for i in week.xi) + ctx.per_gw.loc[week.captain, ctx.gw]
    return [doc.Heading(f"Lineup for GW{ctx.gw}: {xp:.1f} expected with captain")] + \
        recommend.lineup_blocks(week, ctx.per_gw, ctx.pf, ctx.players, ctx.season.teams, payload.get("chip"))


def final_check(ctx, record):
    """Lineup and captain against the latest injury news. Returns True if all is well."""
    if "main" not in record["done"]:
        ok, chip = main_run(ctx, job="final")
        return ok, chip
    log_flags(ctx, "final")
    chip = record.get("chip") or ctx.team.active_chip
    if chip in TRANSFER_CHIPS:
        by_id = ctx.players.set_index("id")
        out = [by_id.loc[p["element"], "web_name"] for p in ctx.team.picks
               if p["position"] <= 11 and by_id.loc[p["element"], "status"] != "a"]
        if out:
            email(True, f"ACTION NEEDED: GW{ctx.gw} starters flagged under your {chips.NAMES[chip]}",
                  message(ctx, f"GW{ctx.gw} final check: ACTION NEEDED", [doc.Callout("Action needed", [
                      f"Flagged since the main run: {', '.join(out)}.",
                      f"With a {chips.NAMES[chip]} active the bot leaves the lineup to you."], "bad")], alert=True))
        return True, chip
    settings = optimizer.Settings(frozen_gws={ctx.gw}, triple_captain_gw=ctx.gw if chip == "3xc" else None,
                                  bench_boost_gw=ctx.gw if chip == "bboost" else None)
    plan = optimizer.plan(ctx.plan_xp, ctx.players, ctx.team.squad, ctx.team.bank, ctx.team.free_transfers,
                          ctx.team.sell_prices, settings)
    action = executor.lineup_action(ctx.team, plan.weeks[0], ctx.players, chip, chip_already_set=bool(chip))
    if action.mode == "skip":
        print(f"Final check: {action.why}.")
        return True, chip
    ok = executor.send([action], ctx.token, ctx.deadline, ctx.live, "final", now=ctx.clock())
    problems = []
    if ctx.live and action.outcome == "sent":
        problems = executor.verify([action], data.get(f"my-team/{ctx.team.entry}/", token=ctx.token))
    success = ok and not problems
    blocks = [] if success else [doc.Callout("Action needed", [
        action.outcome if action.outcome.startswith(("FAILED", "not sent")) else "; ".join(problems),
        doc.Tone(f"Make this change yourself on the FPL site before {ctx.local_deadline} ET.", bold=True)], "bad")]
    blocks += context_blocks(ctx) + executor.describe([action], ctx.players, "Change")
    if problems:
        blocks.append(doc.Callout("Checked on FPL afterwards", problems, "bad"))
    blocks += lineup_section(ctx, action.payload)
    msg = message(ctx, f"GW{ctx.gw} final check: {'lineup updated' if success else 'ACTION NEEDED'}", blocks,
                  alert=not success)
    print(doc.to_text(msg))
    if success:
        email(ctx, f"GW{ctx.gw} final check: lineup updated", msg)
    else:
        email(True, f"ACTION NEEDED: GW{ctx.gw} final check couldn't update the lineup", msg)
    return ok and not problems, chip


def check_write(ctx):
    """Re-save the lineup exactly as it is and read it back: proves the login can write, changes nothing.
    Skipped while any chip is active, so a chip can't be undone by mistake."""
    if not ctx.token:
        raise SystemExit("check-write needs the login (FPL_REFRESH_TOKEN).")
    if ctx.team.active_chip:
        print(f"A chip ({ctx.team.active_chip}) is active this gameweek: not re-saving the lineup.")
        return False
    picks = [{k: p[k] for k in ("element", "position", "is_captain", "is_vice_captain")} for p in ctx.team.picks]
    response = data.post(f"my-team/{ctx.team.entry}/", ctx.token, {"chip": None, "picks": picks})
    after = data.get(f"my-team/{ctx.team.entry}/", token=ctx.token)["picks"]
    same = sorted((p["element"], p["position"], p["is_captain"], p["is_vice_captain"]) for p in after) == \
        sorted((p["element"], p["position"], p["is_captain"], p["is_vice_captain"]) for p in picks)
    print(f"FPL answered HTTP {response.status_code}: {response.text[:300]}")
    print("Lineup on FPL afterwards is unchanged." if same else "WARNING: the lineup on FPL changed!")
    return response.status_code in (200, 201, 202, 204) and same


def tick(now, live):
    """Run whichever job is due (schedule.py), and remember that it ran."""
    events = pd.DataFrame(data.get("bootstrap-static/")["events"])
    events["deadline_time"] = pd.to_datetime(events["deadline_time"], utc=True)
    upcoming = events[events.deadline_time > now]
    if upcoming.empty:
        print("Season over.")
        return None
    gw, deadline = int(upcoming.id.min()), upcoming.deadline_time.min()
    record = schedule.load(gw, deadline)
    job = schedule.due(record, deadline, now)
    if job is None:
        schedule.save(record)
        print(f"Nothing due. GW{gw} deadline {deadline.tz_convert(EASTERN):%a %d %b %H:%M} ET, "
              f"{(deadline - now).total_seconds() / 3600:.1f} h away.")
        return None
    print(f"Running the {job} job for GW{gw}.")
    record["running"] = job                  # so a crash can say which job it was
    schedule.save(record)
    ctx = load_context(now, live, need_login=job in ("main", "final"))
    if job == "preview":
        ok, chip = preview(ctx), None
    elif job == "main":
        ok, chip = main_run(ctx)
    else:
        ok, chip = final_check(ctx, record)
    if job == "final" and "main" not in record["done"] and ok:
        record["done"]["main"] = now.isoformat()
    if chip and ok:
        record["chip"] = chip
    record.pop("running", None)
    schedule.save(schedule.mark(record, job, now, ok))
    return job


def saved_schedule():
    return json.loads(schedule.STATE.read_text()) if schedule.STATE.exists() else {}


def near_deadline(now):
    """For a crash before the job is known: is a deadline close enough that you need to hear about it?"""
    try:
        return pd.Timestamp(saved_schedule()["deadline"]) - now < 3 * schedule.H
    except (KeyError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", choices=["dry-run", "tick", "preview", "main", "final", "check-write"],
                        default="dry-run")
    parser.add_argument("--email", action="store_true", help="email the dry run")
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name(".env"))
    live = os.getenv("BOT_LIVE", "").strip().lower() == "true"
    now = pd.Timestamp.now(tz="UTC")
    if not live:
        print("Shadow mode: nothing is sent to FPL (repository variable BOT_LIVE is not true).")
    try:
        if args.job == "tick":
            tick(now, live)
            return
        ctx = load_context(now, live, need_login=args.job in ("main", "final"))
        if ctx is None:
            print("Season over.")
        elif args.job == "dry-run":
            dry_run(ctx, args.email)
        elif args.job == "preview":
            preview(ctx)
        elif args.job == "main":
            main_run(ctx)
        elif args.job == "check-write":
            if not check_write(ctx):
                raise SystemExit("check-write: FPL didn't accept the re-saved lineup (see above).")
        else:
            final_check(ctx, schedule.load(ctx.gw, ctx.deadline))
    except Exception as problem:
        job = saved_schedule().get("running") if args.job == "tick" else args.job
        if job in ("preview", "main", "final") or (args.job == "tick" and near_deadline(now)):
            email(True, f"ACTION NEEDED: the bot's {job} run crashed", doc.Message(
                f"The bot's {job} run crashed", "Nothing after the crash was done", [("Needs you", "bad")], [
                    doc.Callout("Action needed", [f"{type(problem).__name__}: {problem}", doc.Tone(
                        "If the deadline is close, check your team on the FPL site yourself.", bold=True)], "bad"),
                    doc.Callout("Where it failed", traceback.format_exc().splitlines()[-12:], None)],
                footer=f"Run: {run_link()}", alert=True))
        raise


if __name__ == "__main__":
    main()
