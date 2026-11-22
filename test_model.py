"""Model tests.  python3 test_model.py  (or pytest). Uses the archive in cache/ (downloads if missing)."""
import numpy as np
import pandas as pd

import backtest
import data
import model

AS_OF = pd.Timestamp("2026-11-20 12:00", tz="UTC")


def test_scoring_constants_reproduce_fpl_points():
    """Rebuild every player-gameweek's points from its stats with our constants: must match FPL exactly."""
    h = data.load_archive("2026-27").history
    pos = h["position"]
    pts = (np.where(h.minutes >= 60, 2, np.where(h.minutes > 0, 1, 0))
           + h.goals_scored * pos.map(model.GOAL_PTS) + 3 * h.assists + h.clean_sheets * pos.map(model.CS_PTS)
           - np.where(pos.isin(["GK", "DEF"]), h.goals_conceded // 2, 0) + np.where(pos == "GK", h.saves // 3, 0)
           + 5 * h.penalties_saved - 2 * h.penalties_missed - h.yellow_cards - 3 * h.red_cards - 2 * h.own_goals
           + h.bonus + np.where(h.defensive_contribution >= pos.map(model.DEFCON_AT), 2, 0))
    assert (pts == h.total_points).all()


def test_return_date_parsing():
    assert model.return_date("Hamstring injury - Expected back 29 Nov", AS_OF) == pd.Timestamp("2026-11-29", tz="UTC")
    assert model.return_date("Suspended until 06 Jan", AS_OF) == pd.Timestamp("2027-01-06", tz="UTC")
    assert model.return_date("Knee injury - Unknown return date", AS_OF) is None
    assert model.return_date("", AS_OF) is None


def test_availability():
    soon, later = pd.Timestamp("2026-11-25", tz="UTC"), pd.Timestamp("2026-12-05", tz="UTC")
    assert model.availability("a", 100, "", soon, 0, AS_OF) == 1.0
    assert model.availability("u", 0, "Has joined X on loan", later, 3, AS_OF) == 0.0
    news = "Calf injury - Expected back 01 Dec"
    assert model.availability("i", 0, news, soon, 0, AS_OF) == 0.0
    assert model.availability("i", 0, news, later, 1, AS_OF) == 1.0
    assert model.availability("d", 75, "Knock - 75% chance of playing", soon, 0, AS_OF) == 0.75
    assert 0.75 < model.availability("d", 75, "Knock", later, 1, AS_OF) < 1.0
    # injured with no date: out now, recovering slowly over the following weeks
    no_date = [model.availability("i", 0, "Knee injury - Unknown return date", soon, k, AS_OF) for k in range(4)]
    assert no_date[0] == 0.0 and no_date == sorted(no_date) and no_date[-1] < 0.8


def _project_gw(season, gw, injure=None):
    cur, prior = data.load_archive(season), data.load_archive(data.previous_season(season))
    history = pd.concat([prior.history, cur.history], ignore_index=True)
    as_of = cur.events.set_index("id").loc[gw, "deadline_time"]
    players = backtest.players_at(history, season, gw)
    if injure is not None:
        players.loc[players.id == injure, ["status", "chance", "news"]] = ["i", 0.0, "Unknown return date"]
    return history, players, cur, as_of


def test_no_peeking_at_future_matches():
    history, players, cur, as_of = _project_gw("2026-27", 5)
    full, _ = model.project(history, players, cur.fixtures, cur.teams, [5, 6], as_of)
    past_only, _ = model.project(history[history.kickoff_time < as_of], players, cur.fixtures, cur.teams, [5, 6], as_of)
    pd.testing.assert_frame_equal(full, past_only)


def test_injured_player_projects_zero_and_totals_are_sane():
    history, players, cur, as_of = _project_gw("2026-27", 5)
    star = history[(history.season == "2026-27") & (history.gw < 5)].groupby("element").total_points.sum().idxmax()
    history, players, cur, as_of = _project_gw("2026-27", 5, injure=star)
    per_gw, _ = model.project(history, players, cur.fixtures, cur.teams, [5], as_of)
    assert per_gw.loc[star, 5] == 0.0
    actual = cur.history[cur.history.gw == 5].total_points.sum()
    assert 0.85 < per_gw[5].sum() / actual < 1.15      # a whole gameweek's points, within 15%
    assert per_gw[5].max() < 12 and per_gw[5].min() >= -0.5


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
