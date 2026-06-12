"""Idempotent schema for the rebuilt system. Run: python -m jobs.schema

All new objects use *_v2 / new names so they coexist with the legacy tables
until cutover. Facts, market snapshots, features, and predictions are four
separate concerns; Taipei time exists only in the app display layer.
"""
from __future__ import annotations

from jobs.db_utils import db_connect
from jobs.teams import seed_rows

SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE IF NOT EXISTS public.schema_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.teams (
  team_abbr   TEXT PRIMARY KEY,
  nba_team_id BIGINT NOT NULL UNIQUE,
  full_name   TEXT NOT NULL,
  espn_abbr   TEXT NOT NULL,
  conference  TEXT
);

CREATE TABLE IF NOT EXISTS public.games_v2 (
  game_id      TEXT PRIMARY KEY,
  season       TEXT NOT NULL,
  season_type  TEXT NOT NULL,
  game_date_et DATE NOT NULL,
  tipoff_utc   TIMESTAMPTZ,
  home_abbr    TEXT NOT NULL REFERENCES public.teams(team_abbr),
  away_abbr    TEXT NOT NULL REFERENCES public.teams(team_abbr),
  status       TEXT NOT NULL DEFAULT 'scheduled',
  home_score   INTEGER,
  away_score   INTEGER,
  margin       INTEGER,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_games_v2_date   ON public.games_v2 (game_date_et);
CREATE INDEX IF NOT EXISTS idx_games_v2_season ON public.games_v2 (season, season_type);

CREATE TABLE IF NOT EXISTS public.team_game_stats (
  game_id    TEXT NOT NULL REFERENCES public.games_v2(game_id),
  team_abbr  TEXT NOT NULL REFERENCES public.teams(team_abbr),
  is_home    BOOLEAN NOT NULL,
  wl         TEXT,
  min        INTEGER,
  pts INTEGER, fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
  ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER, reb INTEGER,
  ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  plus_minus INTEGER,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, team_abbr)
);
CREATE INDEX IF NOT EXISTS idx_tgs_team ON public.team_game_stats (team_abbr);

CREATE TABLE IF NOT EXISTS public.player_game_stats (
  game_id     TEXT NOT NULL REFERENCES public.games_v2(game_id),
  player_id   BIGINT NOT NULL,
  team_abbr   TEXT NOT NULL REFERENCES public.teams(team_abbr),
  player_name TEXT,
  min_played  DOUBLE PRECISION,
  pts INTEGER, fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
  ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER, reb INTEGER,
  ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER,
  plus_minus  INTEGER,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_pgs_player    ON public.player_game_stats (player_id);
CREATE INDEX IF NOT EXISTS idx_pgs_team_game ON public.player_game_stats (team_abbr, game_id);

-- Append-only. Open = first row per (game, book); close = last row before tipoff.
CREATE TABLE IF NOT EXISTS public.market_lines (
  line_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  game_id     TEXT NOT NULL REFERENCES public.games_v2(game_id),
  captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source      TEXT NOT NULL,             -- 'oddsapi' | 'manual' | 'legacy'
  book        TEXT,
  home_spread DOUBLE PRECISION NOT NULL, -- home handicap (negative = home favored)
  home_price  DOUBLE PRECISION,          -- decimal odds
  away_price  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_ml_game_time ON public.market_lines (game_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS public.injury_snapshots (
  snapshot_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_date_et DATE NOT NULL,
  captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  team_abbr        TEXT NOT NULL REFERENCES public.teams(team_abbr),
  player_name      TEXT NOT NULL,
  espn_player_id   BIGINT,
  status           TEXT,
  detail           TEXT,
  source           TEXT NOT NULL DEFAULT 'espn',
  UNIQUE (snapshot_date_et, source, team_abbr, player_name)
);
CREATE INDEX IF NOT EXISTS idx_inj_date ON public.injury_snapshots (snapshot_date_et);

-- Audit store of feature vectors used at predict time; training rebuilds in memory.
CREATE TABLE IF NOT EXISTS public.game_features (
  game_id     TEXT NOT NULL REFERENCES public.games_v2(game_id),
  feature_set TEXT NOT NULL,
  features    JSONB NOT NULL,
  built_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (game_id, feature_set)
);

-- Append-only model outputs; line columns denormalized so each prediction is
-- auditable standalone.
CREATE TABLE IF NOT EXISTS public.predictions (
  prediction_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  game_id          TEXT NOT NULL REFERENCES public.games_v2(game_id),
  model_name       TEXT NOT NULL,
  model_version    TEXT NOT NULL,
  predicted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  line_id_used     BIGINT REFERENCES public.market_lines(line_id),
  home_spread_used DOUBLE PRECISION,
  home_price_used  DOUBLE PRECISION,
  away_price_used  DOUBLE PRECISION,
  pred_margin      DOUBLE PRECISION NOT NULL,
  p_raw            DOUBLE PRECISION,     -- uncalibrated cover prob (calibrator training input)
  p_home_cover     DOUBLE PRECISION,
  edge_prob        DOUBLE PRECISION,
  ev_home          DOUBLE PRECISION,
  pick_side        TEXT,                 -- 'HOME' | 'AWAY' | NULL (abstain)
  abstain_reason   TEXT,
  is_paper         BOOLEAN NOT NULL DEFAULT FALSE,
  settled_at       TIMESTAMPTZ,
  cover_result     SMALLINT              -- vs home_spread_used: 1 home, 0 away, 2 push
);
CREATE INDEX IF NOT EXISTS idx_pred_game ON public.predictions (game_id, predicted_at DESC);

CREATE TABLE IF NOT EXISTS public.model_registry_v2 (
  model_name     TEXT NOT NULL,
  model_version  TEXT NOT NULL,
  payload_base64 TEXT NOT NULL,
  feature_set    TEXT,
  feature_names  JSONB,
  trained_rows   INTEGER,
  metrics        JSONB,                  -- MUST include "sigma" for the margin model
  is_active      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (model_name, model_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_active
  ON public.model_registry_v2 (model_name) WHERE is_active;
"""

# Book preference order must match jobs.config.CONFIG.BOOK_PREFERENCE.
VIEWS = """
CREATE OR REPLACE VIEW public.v_latest_lines AS
SELECT DISTINCT ON (ml.game_id) ml.*
FROM public.market_lines ml
ORDER BY ml.game_id,
         (ml.source = 'manual') DESC,
         array_position(ARRAY['pinnacle','draftkings','fanduel','betmgm','caesars','pointsbetus'],
                        ml.book) NULLS LAST,
         ml.captured_at DESC;

CREATE OR REPLACE VIEW public.v_closing_lines AS
SELECT DISTINCT ON (ml.game_id) ml.*
FROM public.market_lines ml
JOIN public.games_v2 g ON g.game_id = ml.game_id
WHERE ml.source <> 'manual'
  AND (g.tipoff_utc IS NULL OR ml.captured_at <= g.tipoff_utc)
ORDER BY ml.game_id,
         array_position(ARRAY['pinnacle','draftkings','fanduel','betmgm','caesars','pointsbetus'],
                        ml.book) NULLS LAST,
         ml.captured_at DESC;

CREATE OR REPLACE VIEW public.v_app_board AS
SELECT g.game_id, g.season, g.season_type, g.game_date_et, g.tipoff_utc, g.status,
       g.home_abbr, g.away_abbr, g.home_score, g.away_score, g.margin,
       l.home_spread, l.home_price, l.away_price, l.source AS line_source, l.book,
       l.captured_at AS line_captured_at,
       p.model_name, p.model_version, p.predicted_at, p.home_spread_used,
       p.pred_margin, p.p_home_cover, p.edge_prob, p.ev_home, p.pick_side,
       p.abstain_reason, p.is_paper, p.cover_result
FROM public.games_v2 g
LEFT JOIN public.v_latest_lines l ON l.game_id = g.game_id
LEFT JOIN LATERAL (
  SELECT * FROM public.predictions p2
  WHERE p2.game_id = g.game_id
  ORDER BY p2.predicted_at DESC
  LIMIT 1
) p ON TRUE;
"""

SEED_TEAMS_SQL = """
INSERT INTO public.teams (team_abbr, nba_team_id, full_name, espn_abbr, conference)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (team_abbr) DO UPDATE SET
  nba_team_id = EXCLUDED.nba_team_id,
  full_name   = EXCLUDED.full_name,
  espn_abbr   = EXCLUDED.espn_abbr,
  conference  = EXCLUDED.conference
"""


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
        for row in seed_rows():
            cur.execute(SEED_TEAMS_SQL, row)
        cur.execute(VIEWS)
        cur.execute(
            """
            INSERT INTO public.schema_meta (key, value, updated_at)
            VALUES ('schema_version', %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (SCHEMA_VERSION,),
        )
    conn.commit()


def main() -> None:
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.teams")
            n_teams = cur.fetchone()[0]
        print(f"[OK] schema ensured (version={SCHEMA_VERSION}, teams={n_teams})", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
