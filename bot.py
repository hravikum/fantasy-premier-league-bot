"""One run of the bot: projections, the plan, the report, and what it would change on FPL.

python3 bot.py            dry run: the report plus the exact requests it would send (sends nothing)
python3 bot.py --email    same, emailed

Logs in with the saved refresh token (auth.py) when it can. Without a login it falls back to
public data for FPL_TEAM_ID, which shows the squad as of the last deadline.
"""
import argparse
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import auth
import data
import executor
import model
import recommend
import team as team_module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", action="store_true")
    parser.add_argument("--horizon", type=int, default=6)
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name(".env"))

    season = data.load_live()
    history = data.load_with_prior(season)
    now = pd.Timestamp.now(tz="UTC")
    upcoming = season.events[season.events.deadline_time > now]
    if upcoming.empty:
        print("Season over.")
        return
    next_gw = int(upcoming.id.min())
    deadline = upcoming.deadline_time.min()
    gws = list(range(next_gw, min(next_gw + args.horizon, 39)))
    per_gw, pf = model.project(history, season.players, season.fixtures, season.teams, gws, now)

    try:
        my_team = team_module.load_private(auth.access_token(), season.players)
        login = "Logged in: squad, selling prices and free transfers are live from FPL."
    except auth.LoginNeeded as problem:
        team_id = os.getenv("FPL_TEAM_ID")
        if not team_id:
            raise SystemExit(f"Not logged in ({problem}) and FPL_TEAM_ID isn't set either.")
        my_team = team_module.load(int(team_id), season.players)
        login = f"NOT LOGGED IN: {problem}\nUsing public data, which shows the squad as of the last deadline."

    text, base, wait, gain = recommend.build(per_gw, pf, season.players, season.fixtures, season.teams, my_team, gws)
    actions = executor.decide(my_team, base.weeks[0], wait.weeks[0], gain, season.players)
    hours = (deadline - now).total_seconds() / 3600
    report = "\n\n".join([f"{login}\nGW{next_gw} deadline in {hours:.0f} hours.", text,
                          executor.describe(actions, season.players)])
    print(report)
    if args.email:
        import mailer
        local = deadline.tz_convert("America/New_York").strftime("%a %d %b %H:%M")
        asks = sum(a.mode == "ask" for a in actions)
        mailer.send(f"GW{next_gw} plan (deadline {local})" + (f", {asks} thing(s) need your OK" if asks else ""), report)


if __name__ == "__main__":
    main()
