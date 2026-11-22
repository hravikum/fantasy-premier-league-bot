"""Replay past gameweeks: project each one from data before its deadline, compare with what happened.

python3 backtest.py                 2025-26 (all 38) and 2026-27 so far
python3 backtest.py 2025-26 10 20   one season, a range of gameweeks
"""
import sys

import numpy as np
import pandas as pd

import data
import model


def players_at(history, season, gw):
    """The player list as it stood at a past gameweek (status unknown, so everyone is available)."""
    rows = history[(history.season == season) & (history.gw == gw)].drop_duplicates("element")
    return pd.DataFrame({
        "id": rows.element, "code": rows.code, "web_name": rows.element.astype(str),
        "position": rows.position, "team": rows.team, "price": rows.value / 10,
        "status": "a", "chance": 100.0, "news": "", "pen_order": rows.pen_order,
    })


def run(season, gws=None, params=model.Params(), quiet=False):
    current = data.load_archive(season)
    prior = data.load_archive(data.previous_season(season))
    history = pd.concat([prior.history, current.history], ignore_index=True)
    gws = gws or sorted(current.history.gw.unique())[1:]
    results = []
    for gw in gws:
        as_of = current.events.set_index("id").loc[gw, "deadline_time"]
        players = players_at(history, season, gw)
        per_gw, _ = model.project(history, players, current.fixtures, current.teams, [gw], as_of, params)
        actual = current.history[current.history.gw == gw].groupby("element").agg(
            actual=("total_points", "sum"), minutes=("minutes", "sum"), fixtures=("fixture", "size"),
            position=("position", "first"), price=("value", "first"), code=("code", "first"))
        frame = actual.join(per_gw[gw].rename("xp"))
        # Baseline: FPL-style form, points per match over the last 4 team fixtures, per fixture this GW.
        # (The archive's own "xP" column was recorded after the matches, so it isn't a fair benchmark.)
        before = history[history.kickoff_time < as_of]
        form = before.groupby("code").tail(4).groupby("code")["total_points"].mean()
        frame["form"] = frame["code"].map(form).fillna(0) * frame["fixtures"]
        frame["gw"] = gw
        results.append(frame.dropna(subset=["xp"]))
        if not quiet:
            print(f"  {season} GW{gw:>2}: {summary(frame)}", flush=True)
    return pd.concat(results)


def summary(frame):
    out = []
    for col in ("xp", "form"):
        err = frame[col] - frame.actual
        top = frame.groupby("gw").apply(lambda g: g.nlargest(20, col).actual.mean(), include_groups=False).mean()
        out.append(f"{col} rmse {np.sqrt((err ** 2).mean()):.3f} r {frame[col].corr(frame.actual):.3f} top20/gw {top:.2f}")
    return " | ".join(out)


def report(frame, label):
    print(f"\n{label}: {frame.gw.nunique()} gameweeks, {len(frame)} player-gameweeks")
    for name, part in [("all players", frame), ("played", frame[frame.minutes > 0]),
                       ("price >= 6.0", frame[frame.price >= 60])]:
        print(f"  {name:<13} {summary(part)}")
    print("  by position (r):", {pos: f"{g.xp.corr(g.actual):.3f} vs {g.form.corr(g.actual):.3f}"
                                 for pos, g in frame.groupby("position")})
    # Calibration: actual points against projected, in bands of projection.
    bands = pd.cut(frame.xp, [-1, 1, 2, 3, 4, 5, 6, 8, 30])
    print("  calibration (mean projected -> mean actual):")
    print(frame.groupby(bands, observed=True).agg(n=("xp", "size"), xp=("xp", "mean"),
                                                   actual=("actual", "mean")).round(2).to_string())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        season = sys.argv[1]
        gws = list(range(int(sys.argv[2]), int(sys.argv[3]) + 1)) if len(sys.argv) > 3 else None
        report(run(season, gws), season)
    else:
        for season in ("2025-26", "2026-27"):
            report(run(season), season)
