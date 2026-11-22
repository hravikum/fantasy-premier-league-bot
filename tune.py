"""Grid-search the model's settings on past gameweeks.  python3 tune.py

Two separate questions, two metrics:
  minutes settings -> correlation over everyone (who plays dominates)
  rate settings    -> correlation over players who played 60+ minutes (how many points, given they play)
"""
import itertools
import numpy as np
import pandas as pd
import backtest, model

def frame_for(params):
    return pd.concat([backtest.run("2025-26", list(range(3, 39)), params, quiet=True),
                      backtest.run("2026-27", list(range(2, 7)), params, quiet=True)])

def per_gw_corr(frame):
    return frame.groupby("gw").apply(lambda g: g.xp.corr(g.actual), include_groups=False).mean()

def search(grid, metric):
    rows = []
    for values in itertools.product(*grid.values()):
        frame = frame_for(model.Params(**dict(zip(grid, values))))
        rows.append({**dict(zip(grid, values)), "metric": metric(frame),
                     "rmse": np.sqrt(((frame.xp - frame.actual) ** 2).mean())})
        print(rows[-1], flush=True)
    return pd.DataFrame(rows).sort_values("metric", ascending=False)

if __name__ == "__main__":
    print(search({"minutes_half_life": [1.5, 2.0, 3.0]}, per_gw_corr).round(4).to_string())
    played = lambda f: per_gw_corr(f[f.minutes >= 60])
    print(search({"half_life_days": [120, 365], "prior_minutes": [300, 900]}, played).round(4).to_string())
