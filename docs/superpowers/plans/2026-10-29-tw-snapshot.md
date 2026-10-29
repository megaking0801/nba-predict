# 台彩賽前盤快照與比價 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An iPhone Shortcut snapshots the official Taiwan pre-match feed, ESPN and The Odds API into the private data repo, and a read-only module answers, once on 2026-11-30, whether the 21:00 Taiwan price beats the closing reference price.

**Architecture:** The phone does all fetching (Taiwan IP) and uploads raw files through the GitHub contents API. Three small modules in the code repo turn files into numbers: `jobs/tw_parse.py` (raw snapshot → tidy rows), `jobs/tw_price.py` (de-vig, line conversion, EV), `jobs/tw_compare.py` (21:00 selection, closing score, pre-registered verdict, report, CLI). Nothing writes to the database.

**Tech Stack:** Python 3.11, pandas, numpy, pytest; iOS Shortcuts; GitHub REST contents API.

**Spec:** `docs/superpowers/specs/2026-10-29-tw-snapshot-design.md`

## Global Constraints
- Read-only: no `INSERT `/`UPDATE `/`DELETE `/`DROP `/`ALTER `/`commit()`/`executemany`/`ensure_schema` in any new module (scanned by tests). DB only through `webui.build_site.connect_readonly()` for the optional secondary P&L.
- No network in `jobs/tw_*.py`. All fetching is the phone's job.
- Team names only through `jobs/teams.py`; unknown or ambiguous names raise `ValueError`.
- No real Taiwan, ESPN or Odds API data in the code repo; tests use synthetic fixtures only. Real samples live in the private data repo under `tw_snapshots/_samples/`.
- Times: compute in UTC; group by US Eastern game date (`jobs.tz.et_date_of`); Taipei time only for the 21:00 window and file names.
- Pre-registered constants (copy exactly): analysis date `2026-11-30` (Taipei); bet window Taipei `20:45`–`21:30`; closing snapshot ≤ `60` minutes before tip; delay move ≥ `1.0` point spread / ≥ `1.5` points total; ≥ `150` bets; ≥ `20` ET game days; ≥ `3` bets per week; `level=0.99`, `block_days=1`, `reps=10000`, `seed=0`; σ spread `(11.0, 12.5, 14.0)` with primary `12.5`; σ total `(17.0, 19.0, 21.0)` with primary `19.0`; no-conversion subset: |Taiwan line − closing reference line| ≤ `0.5`.
- Reference price priority: Pinnacle (Odds API) → median de-vigged probability over Odds API books → ESPN DraftKings. Every bet records its `ref_source`.
- Commit messages in English, end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push without the user's consent.

## Review Focus
1. A day with no 21:00 snapshot (phone missed it): no bets that day; never fall back to another slot.
2. A game the phone never captured within 60 minutes of tip: excluded from the primary sample but listed in the report with its gap.
3. Integer Taiwan lines (e.g. `-5.0`, `230.0`): push probability is split out; EV = p_win·dec + p_push − 1.
4. Taiwan or ESPN team name that does not map: `ValueError` naming the raw string, not a silent drop.
5. A file whose name does not match the snapshot pattern (e.g. `_samples/…`, `.DS_Store`): ignored by discovery, never parsed.

---

### Task 1: Chinese team names in `jobs/teams.py`

**Files:**
- Modify: `jobs/teams.py` (after `abbr_from_team_name`)
- Modify: `jobs/jbot.py:141-146`
- Test: `tests/test_teams_cn.py` (new)

**Interfaces:**
- Produces: `jobs.teams.abbr_from_cn(name: str) -> str` — matches the nickname in `TEAM_NAME_CH` at the end of the stripped name; raises `ValueError(f"unknown Chinese team name: {name!r}")` on zero or several matches.
- Produces: `jobs.teams.require_team_name(name: str) -> str` — `abbr_from_team_name` that raises `ValueError` instead of returning `None` (for Odds API names).

- [ ] **Step 1: Write the failing tests**
```python
def test_abbr_from_cn_matches_by_nickname():
    assert teams.abbr_from_cn("克里夫蘭騎士") == "CLE"
    assert teams.abbr_from_cn(" 紐奧良鵜鶘 ") == "NOP"
    assert teams.abbr_from_cn("洛杉磯快艇") == "LAC" and teams.abbr_from_cn("洛杉磯湖人") == "LAL"

@pytest.mark.parametrize("bad", ["不明球隊", "", None])
def test_abbr_from_cn_raises_on_unknown(bad):
    with pytest.raises(ValueError):
        teams.abbr_from_cn(bad)

def test_require_team_name():
    assert teams.require_team_name("Portland Trail Blazers") == "POR"
    with pytest.raises(ValueError):
        teams.require_team_name("Paris Basketball")
```
- [ ] **Step 2: Run** `python -m pytest tests/test_teams_cn.py -q` — Expected: FAIL (`AttributeError`).
- [ ] **Step 3: Implement both functions in `jobs/teams.py`.** Then make `jbot.team_abbr_from_cn` call `abbr_from_cn` and return `None` on `ValueError`, so jbot keeps its documented contract (`tests/test_jbot.py:210-213` must still pass).
- [ ] **Step 4: Run** `python -m pytest tests/test_teams_cn.py tests/test_jbot.py -q` — Expected: all pass.
- [ ] **Step 5: Commit** `Add Chinese team-name lookup to jobs.teams; jbot delegates to it`.

### Task 2: Snapshot parsing — `jobs/tw_parse.py`

**Files:**
- Create: `jobs/tw_parse.py`
- Create: `tests/fixtures/tw/` with synthetic `20261029T2100+0800-tw.json`, `…-espn.json`, `…-oddsapi.json` (hand-written in the shapes of spec §3; two NBA games, one non-NBA game, one game with an integer spread line)
- Test: `tests/test_tw_parse.py`

**Interfaces:**
- Produces: `SNAPSHOT_RE = r"^(\d{8}T\d{4}[+-]\d{4})-(tw|espn|oddsapi)\.json$"`
- Produces: `list_snapshots(root: Path) -> pd.DataFrame` columns `stamp_utc` (tz-aware UTC), `kind`, `path`; skips non-matching names and any path under `_samples/`.
- Produces: `parse_tw(raw: bytes) -> pd.DataFrame`, one row per selection, columns `home, away` (abbr), `kickoff_utc`, `er`, `market` (`"spread"|"total"|"ml"`), `line` (home handicap for spread, total for total, NaN for ml), `side` (`"home"|"away"|"over"|"under"`), `dec`, `updated_utc`, `m` (int). Only `tn == "美國職籃"`; only markets named `不讓分`, `[總分]讓分 …`, `[總分]大小 …`.
- Produces: `parse_espn(raw: bytes) -> pd.DataFrame`, one row per game: `home, away, tipoff_utc, spread_line` (home), `spread_home_dec, spread_away_dec, total_line, over_dec, under_dec` (all from `close`), plus the same from `open` with `_open` suffix.
- Produces: `parse_oddsapi(raw: bytes) -> pd.DataFrame`, one row per (game, book, market, side): `home, away, tipoff_utc, book, market` (`"spread"|"total"`), `line` (home handicap / total), `side`, `dec`.

- [ ] **Step 1: Write the failing tests** (names and key assertions):
  - `test_list_snapshots_parses_offset_and_skips_others`: a `+0800` stamp `20261029T2100` → `2026-10-29 13:00 UTC`; `_samples/x-tw.json` and `notes.txt` absent.
  - `test_parse_tw_strips_bom_and_converts_prices`: `pu="13", pd="10"` → `dec == 2.3`; non-NBA game absent; three spread lines for one game.
  - `test_parse_tw_spread_sign_is_home_handicap`: option `"紐奧良鵜鶘 -5.5"` with `hv="-5.5"` (home NOP) → row `side == "home", line == -5.5`; `"沙加緬度國王 +5.5"` → `side == "away", line == -5.5`.
  - `test_parse_tw_raises_when_name_sign_disagrees_with_hv`: home option named `+5.5` with `hv="-5.5"` → `ValueError`.
  - `test_parse_tw_unknown_team_raises`.
  - `test_parse_espn_reads_close_and_open_and_checks_provider`: `-110` → `1.909…`; `"o239.5"` → `239.5`; provider `"Caesars"` → `ValueError`; ESPN `"NO"` → `"NOP"` via `normalize_espn_abbr`.
  - `test_parse_oddsapi_rows`: Pinnacle home `-5.5 @ 1.95`, Over `237.5 @ 1.91`; English names through `require_team_name`.
- [ ] **Step 2: Run** `python -m pytest tests/test_tw_parse.py -q` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement `jobs/tw_parse.py`.** Decode with `utf-8-sig`. Side of a Taiwan option = which of `abbr_from_cn(game.hn)` / `abbr_from_cn(game.an)` matches the team part of `name`; for totals, `name` starts with `大` → over, `小` → under. Reuse `jobs.playbook.american_to_decimal`.
- [ ] **Step 4: Run** `python -m pytest tests/test_tw_parse.py -q` — Expected: PASS.
- [ ] **Step 5: Check against real data (manual, not committed).** Copy the session's real Taiwan file into the local data repo: `mkdir -p data/tw_snapshots/_samples && cp <scratchpad>/pre_bk.json data/tw_snapshots/_samples/20261029T1635+0800-tw.json`, and save one ESPN scoreboard the same way. Run `parse_tw` / `parse_espn` on them in a Python shell; confirm NOP is home at −5.5 for 國王 @ 鵜鶘 and matches ESPN `NO -5.5`. Commit in the data repo; pushing it needs the user's consent.
- [ ] **Step 6: Commit** `Add jobs.tw_parse: Taiwan, ESPN and Odds API snapshot parsers`.

### Task 3: Prices — `jobs/tw_price.py`

**Files:**
- Create: `jobs/tw_price.py`
- Test: `tests/test_tw_price.py`

**Interfaces:**
- Produces: `devig(dec_a: float, dec_b: float) -> float` — multiplicative; probability of side a.
- Produces: `implied_mean(ref_line: float, p_ref: float, sigma: float, kind: str) -> float` — `kind="spread"`: home margin mean `μ = −L + σ·Φ⁻¹(p_home_cover)`; `kind="total"`: total mean `μ = L + σ·Φ⁻¹(p_over)`.
- Produces: `outcome_probs(line: float, mu: float, sigma: float, kind: str, side: str) -> tuple[float, float, float]` — `(p_win, p_push, p_loss)`; half-point lines have `p_push == 0`; integer lines use ±0.5 continuity correction.
- Produces: `ev(dec: float, p_win: float, p_push: float) -> float` — `p_win*dec + p_push - 1`.

- [ ] **Step 1: Write the failing tests:**
  - `test_devig_even`: `devig(1.909, 1.909) == pytest.approx(0.5)`.
  - `test_matches_jbot_at_minus_110`: for `L_ref=-5.5`, `L_tw=-4.5`, both sides `1.909`, σ 12.5: home `ev` from this module equals `jbot.spread_ev(-4.5, 1.72, 1.72, -5.5)["ev_home"]` to 1e-9; same check for totals with `jbot.total_ev`.
  - `test_integer_line_has_push`: `outcome_probs(-5.0, mu=5.0, sigma=12.5, kind="spread", side="home")` has `p_push ≈ 0.0319` (Φ(0.04)−Φ(−0.04)) and the three sum to 1.
  - `test_away_and_under_are_complements` for half lines.
- [ ] **Step 2: Run** `python -m pytest tests/test_tw_price.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** with `statistics.NormalDist` (no scipy dependency).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `Add jobs.tw_price: de-vig, line conversion and push-aware EV`.

### Task 4: Reference prices, 21:00 selection and closing score — `jobs/tw_compare.py` (core)

**Files:**
- Create: `jobs/tw_compare.py`
- Test: `tests/test_tw_compare.py` (synthetic snapshot directories built in `tmp_path`)

**Interfaces:**
- Consumes: Task 2 parsers and `list_snapshots`; Task 3 functions.
- Produces: the pre-registered constants of Global Constraints as module-level names: `ANALYSIS_DATE_TPE, BET_WINDOW_TPE, CLOSE_MAX_MIN, DELAY_SPREAD_PTS, DELAY_TOTAL_PTS, MIN_BETS, MIN_DAYS, MIN_BETS_PER_WEEK, LEVEL, BLOCK_DAYS, REPS, SEED, SIGMAS_SPREAD, SIGMAS_TOTAL, SIGMA_SPREAD, SIGMA_TOTAL, NO_CONVERSION_PTS`.
- Produces: `reference(stamp_utc, snaps: pd.DataFrame) -> pd.DataFrame` — per game at that snapshot: `home, away, tipoff_utc, spread_line, p_home_cover, total_line, p_over, ref_source` (`"pinnacle"|"median"|"draftkings"`).
- Produces: `candidates(snaps, sigma_spread, sigma_total) -> pd.DataFrame` — one row per Taiwan spread/total option in the 21:00 snapshot of each ET day: `game, et_date, market, line, side, dec, m, ev_21, ev_close, close_gap_min, close_ref_line, ref_source`.
- Produces: `pick_bets(cands: pd.DataFrame) -> pd.DataFrame` — highest `ev_21` per game, kept only if `ev_21 > 0`.

- [ ] **Step 1: Write the failing tests:**
  - `test_constants_are_preregistered`: assert every constant equals the Global Constraints value.
  - `test_reference_prefers_pinnacle_then_median_then_dk`.
  - `test_bet_window`: a `20:44` Taipei Taiwan file is not a 21:00 snapshot; `21:30` is; a day without one yields zero candidates even when a `06:00` file exists.
  - `test_close_is_last_reference_before_tip_within_60_min`: references at tip−90 and tip−20 → close uses tip−20 and `close_gap_min == 20`; only tip−90 present → game excluded from the primary sample but present with `close_gap_min == 90`.
  - `test_pick_uses_only_21_info`: construct a case where the best option by `ev_close` differs from the best by `ev_21`; `pick_bets` returns the `ev_21` best.
  - `test_et_date_from_tipoff`: tip `2026-10-30 00:00 UTC` → `et_date == 2026-10-29`.
- [ ] **Step 2: Run** `python -m pytest tests/test_tw_compare.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Game matching: (home, away) equal and tip times within 3 hours; ESPN tip time wins. Postponed games (no reference after the 21:00 snapshot and no ESPN `tipoff_utc` within 36 hours) are dropped and listed.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** `Add jobs.tw_compare core: reference prices, 21:00 selection, closing score`.

### Task 5: Verdict, controls, exploratory tables, report and CLI

**Files:**
- Modify: `jobs/tw_compare.py`
- Test: `tests/test_tw_compare.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `verdict(bets: pd.DataFrame, by_sigma: dict[tuple[float, float], pd.DataFrame]) -> dict` with keys `n_bets, n_days, bets_per_week, mean_ev_close, ci_lo, ci_hi, t_lo, t_hi, sigma_lows, no_conversion_mean, passes` (bool) and `reasons` (list of failed conditions). CI from `jobs.feature_ablation.block_bootstrap_ci(d, reps=REPS, seed=SEED, level=LEVEL, block_days=BLOCK_DAYS)` with `d` columns `date`, `d`. `by_sigma` has exactly three keys, pairing the σ tuples by position: `(11.0, 17.0)`, `(12.5, 19.0)`, `(14.0, 21.0)`; the primary pair is `(12.5, 19.0)`.
- Produces: `report(root: Path, as_of_tpe: str) -> dict` — keys `verdict, progress_only` (true before `ANALYSIS_DATE_TPE`; then `verdict` holds counts only, no CI), `by_close_gap`, `by_m` (`"m=1"`, `"m>=2"`), `controls` (`random_pick` with seed 0 over all spread/total options, `main_line_both_sides`), `excluded` (games with their reasons), `delay_events`, `other_slots`, `moneyline`, `notes` (fixed strings: not proven profitable; cannot be followed live yet; single-bet EV ≠ parlay EV).
- Produces: `main(argv: list[str] | None = None) -> int` — `python -m jobs.tw_compare --root data/tw_snapshots --out <file.json> [--as-of YYYY-MM-DD] [--with-results]`; `--with-results` adds the secondary P&L through `connect_readonly()`; DSN never printed (same `_scrub` pattern as `jobs.playbook`).

- [ ] **Step 1: Write the failing tests:**
  - `test_verdict_fails_below_150_bets` (`passes is False`, reason mentions 150).
  - `test_verdict_fails_below_3_per_week` and `test_verdict_fails_below_20_days`.
  - `test_verdict_requires_every_sigma_low_above_zero`: one σ pair with negative low → `passes is False`.
  - `test_verdict_requires_no_conversion_subset_positive`.
  - `test_verdict_passes_on_clear_edge`: 200 bets over 40 days, `EV_close` ~ +0.05 ± 0.02 under every σ → `passes is True`.
  - `test_report_is_progress_only_before_analysis_date`: `as_of_tpe="2026-11-15"` → `progress_only is True` and no `ci_lo` key.
  - `test_main_line_control_is_near_minus_vig`: Taiwan 1.72/1.72 on the reference line → control mean ≈ `1.72*0.5-1 = -0.14`.
  - `test_module_is_read_only` (scan `jobs/tw_compare.py`, `jobs/tw_parse.py`, `jobs/tw_price.py` with the same word list as `tests/test_playbook.py`) and `test_no_network_imports` (`requests`, `urllib.request`, `jobs.espn_http` absent from the three files).
  - `test_cli_error_does_not_leak_a_dsn` (copy of the playbook test, with `--with-results`).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.** Delay events (exploratory): consecutive reference snapshots where both Pinnacle and DraftKings main lines move ≥ the thresholds and the Taiwan option nearest the earlier reference line keeps the same `updated_utc`.
- [ ] **Step 4: Run** `python -m pytest tests -q` — Expected: all pass (count = previous total + new tests).
- [ ] **Step 5: Commit** `Add the pre-registered verdict, controls and report to jobs.tw_compare`.

### Task 6: Shortcut guide, rules and handoff

**Files:**
- Create: `docs/tw-shortcut-setup.md`
- Modify: `CLAUDE.md` (資料與 DB section, and 指令 list)
- Modify: `docs/handoff/2026-10-29-handoff.md` (append a section) — or a new dated handoff if the date has changed

**Interfaces:** none (documentation).

- [ ] **Step 1: Write `docs/tw-shortcut-setup.md`** in Traditional Chinese, every step tappable:
  1. Create the GitHub fine-grained token: only `nba-predict-data`, Contents read/write, 1-year expiry; keep it only in the Shortcut.
  2. Shortcut actions in order: Current Date → Format Date custom `yyyyMMdd'T'HHmmZ` (stamp) and `yyyyMMdd` (folder date) → ET date rule (if hour ≥ 12 the same date, else Adjust Date −1 day, Format `yyyyMMdd`) → Get Contents of URL (Taiwan feed, GET, no extra headers) → Base64 Encode → Get Contents of URL (PUT to `https://api.github.com/repos/megaking0801/nba-predict-data/contents/tw_snapshots/<folder>/<stamp>-tw.json`, headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, JSON body `message`, `content`, `branch=main`) → same for ESPN (`?dates=<ET date>`) → same for The Odds API (URL from spec §5 step 3b), skipped by an If when the hour is 06.
  3. Automations: Time of Day 21:00, 06:00, 07:00, 08:00, 09:00, 10:00, 11:00 → Run Immediately. Build one first and test it with the phone locked; check the files appear in GitHub; then duplicate.
  4. How to stop: turn off the automations, or revoke the token on GitHub.
  5. If any URL returns 403 or a challenge page: turn the automations off and report; no retries, no workarounds.
  6. Ask the user to read the Taiwan lottery terms page and the NBA spread/total overtime rule once in a browser.
- [ ] **Step 2: Add to `CLAUDE.md`** (one bullet each): Taiwan odds only from the `blob3rd` pre-match feed, only from a Taiwan IP, only by the phone Shortcut, never from GitHub Actions or foreign hosts; the user's 2026-10-29 standing consent for the Shortcut to commit to the data repo; `git pull --rebase` in `data/` before every push (the phone writes to `main` too); The Odds API budget (~360 credits/month on the Shortcut; `USE_ODDS=1` in `daily_nba` would exceed the free 500); add `python -m jobs.tw_compare …` to the 指令 list.
- [ ] **Step 3: After the first real Shortcut upload,** run `parse_tw`, `parse_espn` and `parse_oddsapi` on those three files and confirm sides, signs and team mapping by eye (spec §6 last parsing bullet); record the result in the handoff.
- [ ] **Step 4: Append to the handoff:** Shortcut go-live date, first locked-phone test result, the closing-snapshot bug in `market_lines` (separate issue, spec §3), analysis date 2026-11-30.
- [ ] **Step 5: Run** `python -m pytest tests -q` — Expected: all pass.
- [ ] **Step 6: Commit** `Add the Shortcut setup guide and record the snapshot rules`.
