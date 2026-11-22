"""Team-state tests.  python3 test_team.py  (or pytest)"""
import team


def test_selling_price():
    assert team.selling_price(5.0, 5.0) == 5.0
    assert team.selling_price(5.0, 5.1) == 5.0      # half of 0.1 rounds down to nothing
    assert team.selling_price(5.0, 5.2) == 5.1
    assert team.selling_price(5.0, 5.3) == 5.1
    assert team.selling_price(7.5, 8.4) == 7.9
    assert team.selling_price(6.0, 5.7) == 5.7      # falls are passed on in full


def week(event, transfers=0, cost=0):
    return {"event": event, "event_transfers": transfers, "event_transfers_cost": cost}


def test_free_transfers_bank_up_to_five():
    assert team.free_transfers([week(1, 15)], []) == 1
    assert team.free_transfers([week(1), week(2)], []) == 2
    assert team.free_transfers([week(g) for g in range(1, 10)], []) == 5
    assert team.free_transfers([week(1), week(2, 1)], []) == 1
    assert team.free_transfers([week(1), week(2), week(3, 2)], []) == 1


def test_hits_and_chips():
    # 1 free + 1 paid (cost 4): next week starts again from 1
    assert team.free_transfers([week(1), week(2, 2, 4)], []) == 1
    # Wildcard week: transfers are free and the banked ones stay
    assert team.free_transfers([week(1), week(2), week(3, 11)], [("wildcard", 3)]) == 3
    assert team.free_transfers([week(1), week(2), week(3, 5)], [("freehit", 3)]) == 3


def test_chips_available_per_half():
    t = team.Team(1, "x", [], 0, 1, {}, chips_used=[("wildcard", 3), ("3xc", 25)])
    assert t.chips_available(12) == (["freehit", "bboost", "3xc"], 19)
    assert t.chips_available(22) == (["wildcard", "freehit", "bboost"], 38)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
