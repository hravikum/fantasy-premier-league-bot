"""The live-API code paths (data.load_live, team.load), fed archive records shaped like the API.
python3 test_live_offline.py  (or pytest)"""
import pandas as pd

import data
import model
import team

SEASON = "2026-27"


def fake_api(entry_squad, entry_transfers):
    players = data._archive_csv(SEASON, "players_raw.csv")
    fixtures = data._archive_csv(SEASON, "fixtures.csv")
    gw = data._archive_csv(SEASON, "gws/merged_gw.csv")
    deadlines = pd.to_datetime(fixtures.kickoff_time, utc=True).groupby(fixtures.event).min() - pd.Timedelta("90min")
    events = [{"id": int(e), "deadline_time": t.isoformat(), "finished": int(e) <= 6} for e, t in deadlines.items()]
    by_element = {k: g.drop(columns=["name", "position", "team", "xP"]).to_dict("records") for k, g in gw.groupby("element")}
    picks = [{"element": e, "position": k + 1} for k, e in enumerate(entry_squad)]
    responses = {
        "bootstrap-static/": {"events": events, "teams": data._archive_csv(SEASON, "teams.csv").to_dict("records"),
                              "elements": players.to_dict("records")},
        "fixtures/": fixtures.to_dict("records"),
        "entry/99/": {"name": "Offline FC"},
        "entry/99/history/": {"current": [{"event": g, "event_transfers": 1 if g == 4 else 0,
                                           "event_transfers_cost": 0} for g in range(1, 7)],
                              "chips": [{"name": "bboost", "event": 5}]},
        "entry/99/event/6/picks/": {"picks": picks, "entry_history": {"bank": 13}},
        "entry/99/transfers/": entry_transfers,
    }

    def get(path, session=None, **kwargs):
        if path.startswith("element-summary/"):
            return {"history": by_element.get(int(path.split("/")[1]), [])}
        return responses[path]
    return get


def test_live_loaders_on_archive_shaped_responses():
    archive = data.load_archive(SEASON)
    squad = []
    for pos, quota in {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}.items():
        squad += list(archive.players[archive.players.position == pos].id.head(quota))
    bought = squad[3]
    transfers = [{"element_in": bought, "element_in_cost": 45, "element_out": 1, "event": 4,
                  "time": "2026-09-12T10:00:00Z"}]
    original = data.get
    data.get = fake_api(squad, transfers)
    try:
        season = data.load_live()
        my_team = team.load(99, season.players)
    finally:
        data.get = original

    assert season.name == SEASON
    assert len(season.history) >= 0.95 * len(archive.history)        # same matches, one row each
    assert season.history[["team_code", "opp_code", "position", "code"]].notna().all().all()
    assert set(my_team.squad) == set(squad) and my_team.bank == 1.3
    assert my_team.free_transfers == 5                                # 1 into GW2, +1 per week GW2-6, one used in GW4
    now = season.players.set_index("id").loc[bought, "price"]
    assert my_team.sell_prices[bought] == team.selling_price(4.5, now)
    assert my_team.chips_available(7) == (["wildcard", "freehit", "3xc"], 19)

    as_of = season.events.set_index("id").loc[7, "deadline_time"]
    per_gw, _ = model.project(season.history, season.players, season.fixtures, season.teams, [7, 8], as_of)
    assert per_gw.notna().all().all() and per_gw.values.max() > 4


if __name__ == "__main__":
    test_live_loaders_on_archive_shaped_responses()
    print("ok test_live_loaders_on_archive_shaped_responses")
