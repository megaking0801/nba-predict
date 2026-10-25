"""Taiwan sports-lottery odds from Jbot (https://sportsbot.tech) and the comparison maths (read-only).

    JBOT_TOKEN=... python -m jobs.jbot fetch --dates 2026-10-20 [--mode close] [--max-calls 1]
    python -m jobs.jbot schema  --in data_local/jbot/2026-10-20_close.json
    python -m jobs.jbot analyze --table data_local/jbot_normalised.csv

Rules that come from the provider's terms (as read by the main session on 2026-10-26, not re-verified here):
calls at least 5 seconds apart, a trial key allows 20 calls a day shared by all trial users and only the last
30 days, no automated repetition (this tool is run by hand and has no timer or loop), and the data is for
research and analysis only, so it is saved under data_local/ (git-ignored) and never shown on the site.
The token is read from the JBOT_TOKEN environment variable only; it is never printed, logged or saved.
The structure of the odds blocks (normal / handi / total ...) is NOT assumed here: `fetch` saves the raw
response and `schema` prints its shape; the normalising step is written after looking at a real response.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

BASE_URL = "https://api.sportsbot.tech/v2/odds"
MIN_INTERVAL = 6.0                 # the provider asks for more than 5 seconds between calls
MODES = ("close", "open", "both", "all")
MIN_GAMES_FOR_HINT = 100


class JbotError(RuntimeError):
    """An error whose message is safe to print (it never contains the token)."""


class RateLimiter:
    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self.last: Optional[float] = None

    def wait(self, clock: Callable[[], float], sleeper: Callable[[float], None]) -> None:
        if self.last is not None:
            need = self.min_interval - (clock() - self.last)
            if need > 0:
                sleeper(need)

    def mark(self, clock: Callable[[], float]) -> None:
        self.last = clock()


_LIMITER = RateLimiter()


def token_from_env(env=None) -> str:
    env = os.environ if env is None else env
    t = (env.get("JBOT_TOKEN") or "").strip()
    if not t:
        raise JbotError("JBOT_TOKEN is not set; export your Jbot key in the environment (never paste it in chat or a file).")
    return t


def request_odds(date: str, mode: str, token: str, sport: str = "NBA", opener=urllib.request.urlopen,
                 sleeper=time.sleep, clock=time.monotonic, limiter: Optional[RateLimiter] = None) -> dict:
    if mode not in MODES:
        raise JbotError(f"mode must be one of {MODES}")
    limiter = limiter or _LIMITER
    url = BASE_URL + "?" + urllib.parse.urlencode({"sport": sport, "date": date, "mode": mode})
    req = urllib.request.Request(url, headers={"X-JBot-Token": token, "Accept": "application/json"})
    limiter.wait(clock, sleeper)
    try:
        with opener(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise JbotError(f"HTTP {exc.code} from the odds API") from None      # no message text: it may echo headers
    except Exception as exc:  # noqa: BLE001
        raise JbotError(f"request failed ({type(exc).__name__})") from None
    finally:
        limiter.mark(clock)
    try:
        return json.loads(raw)
    except ValueError:
        raise JbotError("the response was not JSON") from None


def quota_of(data: dict) -> Tuple[Optional[int], Optional[int]]:
    u = (data or {}).get("user") or {}
    return u.get("day_limit"), u.get("day_used")


def schema_of(obj, depth: int = 12):
    """The shape of a JSON value (key names and value types), never the values themselves."""
    if depth <= 0:
        return "..."
    if isinstance(obj, dict):
        return {k: schema_of(v, depth - 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [schema_of(obj[0], depth - 1)] if obj else []
    return type(obj).__name__


def fetch_days(dates: List[str], mode: str, out_dir, token: str, max_calls: int = 1, opener=urllib.request.urlopen,
               sleeper=time.sleep, clock=time.monotonic, limiter: Optional[RateLimiter] = None) -> dict:
    """Save the raw response for each date not already saved; stop at max_calls or when the daily quota is used up."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    calls, stopped = 0, None
    for d in dates:
        path = out_dir / f"{d}_{mode}.json"
        if path.exists():
            continue
        if calls >= max_calls:
            stopped = "max calls"
            break
        data = request_odds(d, mode, token, opener=opener, sleeper=sleeper, clock=clock, limiter=limiter)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        calls += 1
        limit, used = quota_of(data)
        print(f"[OK] {d} {mode}: saved ({'quota ' + str(used) + '/' + str(limit) if limit else 'quota unknown'})", flush=True)
        if limit is not None and used is not None and used >= limit:
            stopped = "daily quota used up"
            break
    return {"calls": calls, "stopped": stopped}


# ----- comparison maths on a normalised table -----

def overround(dec_a: pd.Series, dec_b: pd.Series) -> pd.Series:
    """Two-way overround: 1/a + 1/b - 1 (the bookmaker's margin)."""
    return 1.0 / dec_a + 1.0 / dec_b - 1.0


def _boot_mean(x: np.ndarray, reps: int, seed: int, level: float = 0.95) -> Tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(x, len(x), replace=True).mean() for _ in range(reps)])
    a = (1 - level) / 2
    return float(x.mean()), float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def _best_side_ev(p_home: np.ndarray, dec_home: np.ndarray, dec_away: np.ndarray) -> np.ndarray:
    return np.maximum(dec_home * p_home - 1.0, dec_away * (1.0 - p_home) - 1.0)


def ev_vs_consensus(d: pd.DataFrame, reps: int = 2000, seed: int = 0) -> dict:
    """Expected value per unit of the better side of each game, taking the international consensus
    probability as the truth. Biased upward (the consensus probability is itself an estimate and we pick the
    better side), so only the Taiwan-minus-international difference and a clearly positive interval mean anything."""
    p = d["p_consensus"].to_numpy(dtype=float)
    out = {}
    for name, (h, a) in {"tw": ("tw_ml_home_dec", "tw_ml_away_dec"),
                         "espn": ("espn_ml_home_dec", "espn_ml_away_dec")}.items():
        ev = _best_side_ev(p, d[h].to_numpy(dtype=float), d[a].to_numpy(dtype=float))
        mean, lo, hi = _boot_mean(ev, reps, seed)
        out[name] = {"n": int(len(ev)), "mean_best_side_ev": mean, "lo": lo, "hi": hi,
                     "share_positive_ev": float((ev > 0).mean())}
    diff = (_best_side_ev(p, d["tw_ml_home_dec"].to_numpy(float), d["tw_ml_away_dec"].to_numpy(float))
            - _best_side_ev(p, d["espn_ml_home_dec"].to_numpy(float), d["espn_ml_away_dec"].to_numpy(float)))
    mean, lo, hi = _boot_mean(diff, reps, seed)
    out["tw_minus_espn"] = {"mean": mean, "lo": lo, "hi": hi}
    out["hint_of_value"] = hint_of_value({"n": out["tw"]["n"], "lo": lo})
    return out


def line_difference(d: pd.DataFrame, tw_col: str, espn_col: str) -> dict:
    diff = (d[tw_col].astype(float) - d[espn_col].astype(float)).dropna()
    return {"n": int(len(diff)), "mean_diff": float(diff.mean()) if len(diff) else None,
            "mean_abs_diff": float(diff.abs().mean()) if len(diff) else None,
            "share_same_line": float((diff.abs() < 1e-9).mean()) if len(diff) else None}


def hint_of_value(s: dict) -> bool:
    """Only a clearly positive interval on enough games counts as a hint; it is never proof."""
    return bool(s.get("n", 0) >= MIN_GAMES_FOR_HINT and s.get("lo", -1.0) > 0)


# ----- CLI -----

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.jbot", description="Jbot Taiwan lottery odds (read-only, manual)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--dates", required=True, help="comma-separated YYYY-MM-DD, within the last 30 days")
    f.add_argument("--mode", default="close", choices=MODES)
    f.add_argument("--out-dir", default="data_local/jbot")
    f.add_argument("--max-calls", type=int, default=1)
    s = sub.add_parser("schema")
    s.add_argument("--in", dest="inp", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--table", required=True)
    a.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            res = fetch_days([x.strip() for x in args.dates.split(",") if x.strip()], args.mode, args.out_dir,
                             token_from_env(), max_calls=args.max_calls)
            print(json.dumps(res))
        elif args.cmd == "schema":
            print(json.dumps(schema_of(json.loads(Path(args.inp).read_text(encoding="utf-8"))),
                             indent=1, ensure_ascii=False))
        else:
            d = pd.read_csv(args.table)
            result = {"ev": ev_vs_consensus(d)}
            if {"tw_spread_line", "espn_home_spread"} <= set(d.columns):
                result["spread_line"] = line_difference(d, "tw_spread_line", "espn_home_spread")
            text = json.dumps(result, indent=1, ensure_ascii=False)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            print(text)
    except JbotError as exc:
        print(f"Jbot: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
