"""Pure HTML rendering for the NBA prediction site.

Shared by the Streamlit app and the static site builder. No streamlit import,
no DB access, no wall-clock reads: callers pass data and ``now`` in.
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from jobs.teams import TEAM_NAME_CH

TW = ZoneInfo("Asia/Taipei")


class Links(Protocol):
    def home(self) -> str: ...
    def game(self, game_id: str) -> str: ...
    def team(self, abbr: str) -> str: ...


class StreamlitLinks:
    """Query-string links used by the Streamlit app."""

    def home(self) -> str:
        return "?"

    def game(self, game_id: str) -> str:
        return f"?game={game_id}"

    def team(self, abbr: str) -> str:
        return "?"


STREAMLIT_LINKS = StreamlitLinks()


def esc(x) -> str:
    """HTML-escape DB-sourced text; None/NaN become an empty string."""
    if x is None or (not isinstance(x, str) and pd.isna(x)):
        return ""
    return html.escape(str(x), quote=True)


NEON_CSS = """
<style>
/* v2 (2026-10-29): neutral paper and ink; the one memorable thing is the centre-line win-probability bar
   (fill grows from the half-court line toward the favoured team). Colour only in hit/miss words.
   Mobile first. Dark tokens live in STATIC_EXTRA_CSS (static site only; the Streamlit shell stays light). */
:root {
  --ground: #F2F4F6;          /* paper */
  --surface: #FFFFFF;
  --text: #142033;            /* ink */
  --muted: #566173;
  --faint: #566173;
  --hair: #DCE1E7;            /* lines and the probability track */
  --hair-strong: #C5CCD5;
  --fill: #142033;            /* probability fill = ink */
  --cyan: #142033;            /* legacy name: links and focus use ink */
  --magenta: #142033;
  --violet: #142033;
  --accent-soft: #DCE1E7;
  --win: #1F7A4F;
  --miss: #B4372E;
  --amber: #142033;
  --display: "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
  --led: var(--display);
  --body: var(--display);
}

/* ---- Streamlit shell ---- */
.stApp { color: var(--text); background: var(--ground); }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, [data-testid="stToolbar"] { visibility: hidden; }
.block-container { max-width: 880px; padding-top: 1.25rem; padding-bottom: 3rem; }
.stApp, .stApp p, .stApp span, .stApp div, .stApp label { font-family: var(--body); }
.stApp a { color: var(--text); }
.stApp a:focus-visible, .stApp button:focus-visible, .stApp summary:focus-visible {
  outline: 2px solid var(--text); outline-offset: 2px; }

.led { font-variant-numeric: tabular-nums; font-weight: 600; }

/* ---- top bar and headline ---- */
.nb-topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
             padding: 6px 0 12px; border-bottom: 1px solid var(--hair); }
.nb-brand { font-weight: 700; font-size: 16px; }
.nb-badge { display: flex; align-items: baseline; gap: 6px; font-size: 14px; color: var(--muted); }
.nb-badge b { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 600; }
.nb-hero { padding: 16px 0 4px; }
.nb-hero h1 { font-weight: 700; font-size: 24px; line-height: 1.3; margin: 0 0 6px; }
.nb-lede { max-width: 640px; color: var(--muted); font-size: 15px; margin: 0; line-height: 1.7; }

/* ---- matchup head (detail page) ---- */
.nb-mbig { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px; }
.nb-tb { text-align: center; }
.nb-tb .abbr { font-weight: 700; font-size: 24px; line-height: 1.2; }
.nb-tb .name { color: var(--muted); font-size: 14px; margin-top: 2px; }
.nb-at { color: var(--muted); font-size: 14px; }

/* ---- win probability: numbers at both ends, centre-line bar ---- */
.nb-prob { display: flex; align-items: center; gap: 10px; min-width: 0; }
.nb-pct { font-variant-numeric: tabular-nums; font-size: 18px; font-weight: 600; color: var(--muted); }
.nb-pct.fav { color: var(--text); }
.nb-prob.big .nb-pct { font-size: 24px; }
.nb-mid { position: relative; display: block; flex: 1; height: 8px; min-width: 120px;
          border-radius: 4px; background: var(--hair); }
.nb-mid .fill { position: absolute; top: 0; bottom: 0; background: var(--fill); }
.nb-mid .fill.h { left: 50%; border-radius: 0 4px 4px 0; }
.nb-mid .fill.a { right: 50%; border-radius: 4px 0 0 4px; }
.nb-mid::after { content: ""; position: absolute; left: 50%; top: 50%; width: 10px; height: 10px;
                 transform: translate(-50%, -50%); border-radius: 50%; background: var(--surface);
                 border: 2px solid var(--text); box-sizing: border-box; z-index: 1; }
.nb-prob.big .nb-mid { height: 12px; border-radius: 6px; }
/* detail header: numbers above the two ends, the bar spans the full container width */
.nb-prob.big { display: grid; grid-template-columns: auto 1fr auto; grid-template-areas: "ap . hp" "bar bar bar";
               align-items: end; row-gap: 8px; }
.nb-prob.big > .nb-pct:first-child { grid-area: ap; }
.nb-prob.big > .nb-mid { grid-area: bar; }
.nb-prob.big > .nb-pct:last-child { grid-area: hp; }
.nb-prob.big .nb-mid::after { width: 16px; height: 16px; }

/* ---- section heads ---- */
.nb-sechead { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 4px 12px;
              margin: 8px 0 10px; }
.nb-sechead h2 { font-weight: 700; font-size: 18px; margin: 0; }
.nb-sechead .sub { color: var(--muted); font-size: 14px; }

/* ---- date groups and game rows ---- */
.nb-group { margin-bottom: 20px; }
.nb-datehead { display: flex; align-items: baseline; gap: 10px; margin: 4px 0 8px; }
.nb-datehead .d { font-size: 15px; font-weight: 700; }
.nb-datehead .cnt { font-size: 14px; color: var(--muted); }
.nb-ha { font-size: 13px; font-weight: 400; color: var(--muted); margin: 0 4px; }
.nb-rows { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; }
a.nb-row { display: block; min-height: 56px; box-sizing: border-box; padding: 10px 14px;
           color: inherit; text-decoration: none; border-top: 1px solid var(--hair); }
a.nb-row:first-child { border-top: none; border-radius: 10px 10px 0 0; }
a.nb-row:last-child { border-radius: 0 0 10px 10px; }
a.nb-row:hover { background: var(--ground); }
a.nb-row:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
.nb-rtop { display: flex; justify-content: space-between; font-size: 14px; color: var(--muted); margin-bottom: 4px; }
/* phone: line 1 = away team ... home team, line 2 = 34% [full-width bar] 66%; one line from 720px */
.nb-rmain { display: grid; grid-template-columns: 1fr 1fr; grid-template-areas: "an hn" "pr pr";
            align-items: center; gap: 6px 12px; }
.nb-rmain > .nb-rteam:first-child { grid-area: an; }
.nb-rmain > .nb-rteam.r { grid-area: hn; text-align: right; }
.nb-rmain > .nb-prob { grid-area: pr; }
.nb-rteam { font-size: 15px; font-weight: 600; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.nb-rteam.fav { color: var(--text); }
.nb-rres { margin-top: 6px; display: flex; justify-content: space-between; gap: 8px; font-size: 14px; }
.nb-rres .score { font-variant-numeric: tabular-nums; }
.nb-hit { color: var(--win); font-weight: 600; }
.nb-miss { color: var(--miss); font-weight: 600; }
details.nb-more { margin: 4px 0 20px; }
details.nb-more summary { cursor: pointer; font-size: 15px; padding: 10px 0; text-decoration: underline; }
details.nb-more[open] summary { margin-bottom: 8px; }

/* ---- track record: plain text columns, no big numbers ---- */
.nb-record { display: grid; grid-template-columns: 1fr; gap: 8px; }
.nb-rcell { padding: 0; }
.nb-rcell .k { font-size: 14px; color: var(--muted); }
.nb-rcell .v { font-size: 18px; font-weight: 600; margin: 2px 0; font-variant-numeric: tabular-nums; }
.nb-rcell .v.c, .nb-rcell .v.m { color: var(--text); }
.nb-rcell .note { color: var(--muted); font-size: 14px; }
.nb-honest { margin-top: 12px; color: var(--muted); font-size: 14px; line-height: 1.8; }
.nb-honest b { color: var(--text); }

/* ---- panels and tables ---- */
.nb-tables { display: grid; grid-template-columns: 1fr; gap: 12px; }
.nb-panel { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 12px 14px; }
.nb-panel h3 { margin: 0 0 8px; font-size: 15px; font-weight: 700; }
.nb-panel h3 span { color: var(--muted); font-weight: 400; font-size: 14px; margin-left: 4px; }
.nb-panel .empty { color: var(--muted); font-size: 14px; padding: 4px 0; }
table.ntable { width: 100%; border-collapse: collapse; font-size: 14px; color: var(--text); }
table.ntable thead th { text-align: right; color: var(--muted); font-weight: 600; font-size: 14px; padding: 0 0 6px; }
table.ntable thead th:first-child, table.ntable tbody td:first-child { text-align: left; }
table.ntable tbody td { padding: 6px 0; border-top: 1px solid var(--hair); text-align: right;
                        font-variant-numeric: tabular-nums; }

/* ---- footer ---- */
.nb-disc { color: var(--muted); font-size: 14px; line-height: 1.8; padding-top: 12px; border-top: 1px solid var(--hair); }
.nb-disc b { color: var(--text); }
.nb-sign { margin-top: 8px; color: var(--muted); font-size: 14px; }

[data-testid="stSidebar"] { background: var(--surface); border-right: 1px solid var(--hair); }
.stButton > button { background: var(--text); color: var(--surface); border: none; font-weight: 600; border-radius: 8px; }
[data-baseweb="select"] > div { background: var(--surface); border-color: var(--hair-strong); }

/* ---- detail page ---- */
.nb-back { display: inline-block; padding: 10px 0; color: var(--text); font-size: 15px; }
.nb-detailhead { border-radius: 10px; padding: 14px; margin-top: 8px; background: var(--surface); border: 1px solid var(--hair); }
.nb-detailhead .nb-prob { margin-top: 12px; }
.nb-detailhead .when { text-align: center; font-size: 14px; color: var(--muted); margin-top: 10px; }
.nb-bets { display: grid; grid-template-columns: 1fr; background: var(--surface);
           border: 1px solid var(--hair); border-radius: 10px; }
.nb-bet { padding: 12px 14px; border-top: 1px solid var(--hair); }
.nb-bet:first-child { border-top: none; }
.nb-bet h4 { margin: 0 0 8px; font-size: 15px; font-weight: 700; display: flex; align-items: baseline; gap: 6px; }
.nb-bet h4 span { font-size: 14px; font-weight: 400; color: var(--muted); }
.nb-bet .line { font-size: 14px; margin-bottom: 6px; font-variant-numeric: tabular-nums; }
.nb-bet .line b { color: var(--text); }
.nb-bet .verdict { margin-top: 8px; font-size: 14px; color: var(--muted); }
.nb-bet .verdict b { color: var(--text); }
.nb-bet .res { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--hair); font-size: 14px;
               display: flex; justify-content: space-between; align-items: center; }
.nb-bet.disabled .soon { color: var(--muted); font-size: 14px; line-height: 1.7; }
.nb-bet .empty { color: var(--muted); font-size: 14px; }
.nb-totline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 10px; }
.nb-totline .led.big { font-weight: 700; font-size: 24px; line-height: 1.2; }
.nb-totline .vs { font-size: 14px; color: var(--muted); }
.nb-lean { font-size: 14px; color: var(--muted); }
.nb-caveat { margin-top: 8px; font-size: 14px; line-height: 1.7; color: var(--muted); }
.nb-caveat b { color: var(--text); }
.nb-twocol { display: grid; grid-template-columns: 1fr; gap: 12px; }
.nb-inj { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-top: 1px solid var(--hair); font-size: 14px; }
.nb-inj:first-child { border-top: none; }
.nb-inj .pn { flex: 1; }
.nb-inj .det { color: var(--muted); font-size: 14px; }
.nb-injstatus { font-size: 14px; white-space: nowrap; }
.nb-injstatus.out { color: var(--miss); }
.nb-injstatus.dtd { color: var(--muted); }

@media (min-width: 720px) {
  .nb-rmain { grid-template-columns: minmax(96px, auto) minmax(260px, 1fr) minmax(96px, auto);
              grid-template-areas: "an pr hn"; gap: 12px; }
  .nb-record { grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .nb-tables { grid-template-columns: 1.15fr 1fr; }
  .nb-twocol { grid-template-columns: 1fr 1fr; }
  .nb-bets { grid-template-columns: repeat(3, 1fr); }
  .nb-bet { border-top: none; border-left: 1px solid var(--hair); }
  .nb-bet:first-child { border-left: none; }
  .nb-hero h1 { font-size: 24px; }
}
</style>
"""

DISCLAIMER_HTML = (
    '<div class="nb-disc">⚠ 預測由數據模型產生，<b>僅供球迷參考與娛樂，非投注建議</b>。'
    '模型會猜勝負，但不比盤口準；運彩賠率早已反映強弱，高命中率不等於能獲利。<br>'
    '資料來源：ESPN 的球隊與球員數據（2015-16 球季起）。模型用近況、Elo 實力、'
    '四要素、休息天數等預測雙方分差，再換算成勝率。</div>'
    '<div class="nb-sign">NBA 勝負預測，僅供參考與娛樂</div>'
)


# =========================================================
# Helpers
# =========================================================

_WD = ["一", "二", "三", "四", "五", "六", "日"]


def fmt_team(abbr: str) -> str:
    return esc(TEAM_NAME_CH.get(abbr, abbr))


# ESPN injury text → Chinese. status is exact; detail matches whole phrase first,
# then word-by-word (body part + injury type) so combos like "Knee Surgery" work.
INJ_STATUS_CH = {
    "out": "缺陣", "day-to-day": "每日觀察", "doubtful": "出賽成疑",
    "questionable": "出賽待定", "probable": "大致可上", "available": "可出賽",
    "game time decision": "臨場決定", "suspension": "禁賽", "suspended": "禁賽",
}
INJ_TERM_CH = {
    # injury types
    "surgery": "手術", "sprain": "扭傷", "strain": "拉傷", "not specified": "未說明",
    "soreness": "痠痛", "bruise": "挫傷", "contusion": "挫傷", "fracture": "骨折",
    "pinched nerve": "神經夾擠", "tendinitis": "肌腱炎", "inflammation": "發炎",
    "illness": "生病", "rest": "輪休", "concussion": "腦震盪", "tear": "撕裂",
    "rupture": "斷裂", "spasm": "痙攣", "dislocation": "脫臼", "laceration": "撕裂傷",
    "stinger": "神經拉傷", "tightness": "緊繃", "personal": "個人因素",
    # body parts
    "knee": "膝蓋", "ankle": "腳踝", "hamstring": "腿後肌", "back": "背部",
    "shoulder": "肩膀", "wrist": "手腕", "foot": "足部", "hip": "髖部", "calf": "小腿",
    "groin": "鼠蹊", "achilles": "阿基里斯腱", "hand": "手部", "finger": "手指",
    "toe": "腳趾", "elbow": "手肘", "neck": "頸部", "quad": "股四頭肌",
    "quadriceps": "股四頭肌", "thigh": "大腿", "thumb": "拇指", "rib": "肋骨",
    "abdominal": "腹部", "oblique": "腹斜肌", "heel": "腳跟", "leg": "腿部",
    "arm": "手臂", "chest": "胸部", "nose": "鼻部", "eye": "眼部", "face": "臉部",
}


def tw_status(s) -> str:
    return INJ_STATUS_CH.get(str(s or "").strip().lower(), str(s or ""))


def tw_detail(d) -> str:
    d = str(d or "").strip()
    if not d:
        return ""
    if d.lower() in INJ_TERM_CH:
        return INJ_TERM_CH[d.lower()]
    return " ".join(INJ_TERM_CH.get(t.lower(), t) for t in d.split())


def fmt_when(ts, date_et) -> str:
    if ts is not None and not pd.isna(ts):
        return pd.Timestamp(ts).tz_convert(TW).strftime("%m/%d %H:%M")
    return esc(date_et)


def matchup_block(g, big: bool = False) -> str:
    """Away ＠ Home with 主/客 badges; favored side highlighted in cyan."""
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    away, home = g["away_abbr"], g["home_abbr"]
    tb, at = ("nb-tb", "nb-at") if big else ("t", "at")
    away_cls = " fav" if not home_fav else ""
    home_cls = " fav" if home_fav else ""
    away_name = f'{fmt_team(away)}<span class="nb-ha away">客</span>'
    home_name = f'{fmt_team(home)}<span class="nb-ha home">主</span>'
    return (
        f'<div class="{"nb-mbig" if big else "nb-cmatch"}">'
        f'<div class="{tb}{away_cls}"><div class="abbr">{esc(away)}</div>'
        f'<div class="name">{away_name}</div></div>'
        f'<span class="{at}">＠</span>'
        f'<div class="{tb}{home_cls}"><div class="abbr">{esc(home)}</div>'
        f'<div class="name">{home_name}</div></div>'
        f'</div>'
    )


def prob_bar(g, big: bool = False) -> str:
    """Both win probabilities as numbers, and a bar whose fill starts at the centre line (50%) and
    grows toward the favoured team by how far the game is from a coin flip. The bar is decorative."""
    home_pct = round(float(g["p_home_win"]) * 100)
    away_pct = 100 - home_pct
    a_cls, h_cls = ("", " fav") if home_pct > 50 else ((" fav", "") if home_pct < 50 else ("", ""))
    reach = abs(home_pct - 50)                      # % of the full width; half the track is 50%
    fill = ""
    if reach:
        side = "h" if home_pct > 50 else "a"
        fill = f'<i class="fill {side}" style="width:{reach}%"></i>'
    size = " big" if big else ""
    return (
        f'<div class="nb-prob{size}">'
        f'<span class="nb-pct{a_cls}">{away_pct}%</span>'
        f'<span class="nb-mid" aria-hidden="true">{fill}</span>'
        f'<span class="nb-pct{h_cls}">{home_pct}%</span>'
        f'</div>'
    )


def _row_label(g) -> str:
    """What a screen reader says for one game row (the visible numbers and bar are hidden from it)."""
    home_pct = round(float(g["p_home_win"]) * 100)
    parts = [f'{TEAM_NAME_CH.get(g["away_abbr"], g["away_abbr"])}（客）{100 - home_pct}%',
             f'{TEAM_NAME_CH.get(g["home_abbr"], g["home_abbr"])}（主）{home_pct}%',
             fmt_when(g["tipoff_utc"], g["game_date_et"]) + " 台北時間",
             "終場" if g["status"] == "final" else "尚未開賽"]
    if g["status"] == "final" and pd.notna(g["win_result"]):
        home_won = g["win_result"] == 1
        parts.append(f'{_pts(g["away_score"])} 比 {_pts(g["home_score"])}')
        parts.append("勝率較高的一方" + ("贏了" if (home_pct >= 50) == home_won else "輸了"))
    return "，".join(parts)


def _pts(v) -> str:
    """A score as a whole number ('110', not '110.0'); blank when unknown."""
    return "" if v is None or pd.isna(v) else str(int(v))


def _result_html(g) -> str:
    """Final score and whether the side with the higher win probability won."""
    if not (g["status"] == "final" and pd.notna(g["win_result"])):
        return ""
    home_won = g["win_result"] == 1
    home_fav = float(g["p_home_win"]) >= 0.5
    win_abbr = g["home_abbr"] if home_won else g["away_abbr"]
    mark = ('<span class="nb-hit">命中</span>' if home_fav == home_won
            else '<span class="nb-miss">未中</span>')
    return (f'<div class="nb-rres"><span class="score">{esc(g["away_abbr"])} {_pts(g["away_score"])} : '
            f'{_pts(g["home_score"])} {esc(g["home_abbr"])}　{fmt_team(win_abbr)}勝</span>{mark}</div>')


def game_card_html(g, links: Links = STREAMLIT_LINKS) -> str:
    """One game as one row: time and state, the two teams with their win probabilities, the result."""
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    state = "終場" if g["status"] == "final" else "尚未開賽"
    a_cls, h_cls = ("", " fav") if home_fav else (" fav", "")
    return (
        f'<a class="nb-row" href="{esc(links.game(g["game_id"]))}" target="_self" aria-label="{esc(_row_label(g))}">'
        f'<div class="nb-rtop"><span class="nb-when">{fmt_when(g["tipoff_utc"], g["game_date_et"])}</span>'
        f'<span class="nb-state">{state}</span></div>'
        f'<div class="nb-rmain">'
        f'<span class="nb-rteam{a_cls}">{fmt_team(g["away_abbr"])}<span class="nb-ha">客</span></span>'
        f'{prob_bar(g)}'
        f'<span class="nb-rteam r{h_cls}">{fmt_team(g["home_abbr"])}<span class="nb-ha">主</span></span>'
        f'</div>'
        f'{_result_html(g)}'
        f'</a>'
    )


def date_group_html(date_val, rows: list, links: Links = STREAMLIT_LINKS) -> str:
    ts = pd.Timestamp(date_val)
    label = ts.strftime("%m/%d")
    wd = f"週{_WD[ts.weekday()]}"
    n = len(rows)
    finals = sum(1 for r in rows if r["status"] == "final")
    if finals == n:
        word = "已結束"
    elif finals == 0:
        word = "尚未開賽"
    else:
        word = f"{n - finals} 場待打"
    games = "".join(game_card_html(r, links) for r in rows)
    return (
        f'<div class="nb-group">'
        f'<div class="nb-datehead"><span class="d">{label} {wd}</span>'
        f'<span class="cnt">{n} 場，{word}</span></div>'
        f'<div class="nb-rows">{games}</div></div>'
    )


def table_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return '<div class="empty">尚無資料</div>'
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(lambda v: esc(v) if isinstance(v, str) else v)
    return out.to_html(index=False, classes="ntable", border=0, escape=False)


def topbar_html(acc, back: bool = False, links: Links = STREAMLIT_LINKS) -> str:
    badge = (f'<div class="nb-badge"><b class="led">{acc * 100:.0f}%</b>'
             f'<span>時間外猜中率</span></div>') if acc else ""
    bar = f'<div class="nb-topbar"><div class="nb-brand">NBA 勝負預測</div>{badge}</div>'
    if back:
        return f'<a class="nb-back" href="{links.home()}" target="_self">返回今天的賽程</a>{bar}'
    return bar


# ----- matchup detail page -----

def _bet_moneyline(g) -> str:
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    fav_abbr = g["home_abbr"] if home_fav else g["away_abbr"]
    side = "主" if home_fav else "客"
    res = ""
    if g["status"] == "final" and pd.notna(g["win_result"]):
        home_won = g["win_result"] == 1
        correct = home_fav == home_won
        win_abbr = g["home_abbr"] if home_won else g["away_abbr"]
        badge = ('<span class="nb-hit">命中</span>' if correct
                 else '<span class="nb-miss">未中</span>')
        res = (f'<div class="res"><span class="score">{fmt_team(win_abbr)} 勝</span>{badge}</div>')
    return (
        '<div class="nb-bet"><h4>勝負 <span>Moneyline</span></h4>'
        f'<div class="verdict">勝率較高：<b>{fmt_team(fav_abbr)}</b>（{side}）</div>'
        f'{res}</div>'
    )


# Measured 2026-10-26 (docs/handoff/2026-10-23-roadmap.md, P5): over the 11 complete seasons the model's
# margin error was 0.36 points worse than the closing line, and its cover probability carried no information
# (calibration slope about -0.03, ATS hit rate about 50%). So this card gives no cover probability and no pick.
SPREAD_CAVEAT = ('⚠ 本站不提供過盤機率或下注推薦：11 個完整球季的時間外驗證中，模型的分差誤差比盤口大約 0.36 分，'
                 '沒有任何一季比盤口準，過盤機率也沒有可用的資訊。')


def _bet_spread(g) -> str:
    hs = g["home_spread"]
    if hs is None or pd.isna(hs):
        return ('<div class="nb-bet"><h4>受讓分 <span>Spread</span></h4>'
                '<div class="empty">這場沒有盤口資料。</div></div>')
    hs = float(hs)
    home, away = g["home_abbr"], g["away_abbr"]
    line = f'客 {esc(away)} {(-hs):+.1f}　／　主 {esc(home)} {hs:+.1f}'
    src = (g["line_source"] or "").upper() or "—"
    pm = g.get("pred_margin")
    est = ""
    if pm is not None and not pd.isna(pm):
        pm = float(pm)
        gap = pm + hs                       # model's home margin minus the line's implied home margin
        if abs(gap) < 0.5:
            gap_txt = "與盤口幾乎一致"
        elif gap > 0:
            gap_txt = f"比盤口多給主隊 {gap:.1f} 分"
        else:
            gap_txt = f"比盤口多給客隊 {-gap:.1f} 分"
        est = (f'<div class="line">模型估計分差 <b>主 {pm:+.1f}</b>　·　{gap_txt}</div>')
    res = ""
    if g["status"] == "final" and pd.notna(g["cover_result"]):
        cr = int(g["cover_result"])
        if cr == 2:
            res = '<div class="res"><span class="score">和盤 push</span><span>退回</span></div>'
        else:
            cov_actual = home if cr == 1 else away
            res = f'<div class="res"><span class="score">{fmt_team(cov_actual)} 破盤</span></div>'
    return (
        '<div class="nb-bet"><h4>受讓分 <span>Spread</span></h4>'
        f'<div class="line">{line}　·　<b>{esc(src)}</b> 盤口</div>'
        f'{est}'
        f'<div class="nb-caveat">{SPREAD_CAVEAT}</div>'
        f'{res}</div>'
    )


def _bet_total(g, over_acc=None) -> str:
    """Estimate-first totals card: lead with the model's projected total vs the
    market line; the over/under *direction* is shown only as a soft lean with an
    honest caveat (walk-forward O/U accuracy is barely above a coin flip)."""
    tl = g.get("total_line")
    pt = g.get("pred_total")
    has_line = tl is not None and not pd.isna(tl)
    has_pred = pt is not None and not pd.isna(pt)
    src = (g.get("line_source") or "").upper() or "—"
    head = '<div class="nb-bet"><h4>大小分 <span>O/U</span></h4>'
    caveat = (
        f'<div class="nb-caveat">⚠ 本卡以「模型估總分」為主。破大／破小方向時間外命中率僅約 '
        f'<b>{over_acc * 100:.0f}%</b>，盤口效率高、難穩定贏盤，方向僅供參考。</div>'
        if over_acc else
        '<div class="nb-caveat">⚠ 本卡以「模型估總分」為主，破大／破小方向僅供參考。</div>'
    )

    if not has_pred:
        if has_line:
            return (f'{head}'
                    f'<div class="line">盤口總分 <b>{float(tl):.1f}</b>　·　<b>{esc(src)}</b></div>'
                    '<div class="empty">本季開賽後顯示模型估總分與大小分傾向。</div>'
                    f'{caveat}</div>')
        return ('<div class="nb-bet disabled"><h4>大小分 <span>O/U</span></h4>'
                '<div class="soon">本季開賽後顯示——模型估總分、盤口線與大小分傾向。</div></div>')

    pt = float(pt)
    pick = None  # None = no comparable line / 貼盤; True = lean over; False = lean under
    if has_line:
        diff = pt - float(tl)
        if abs(diff) < 0.5:
            lean = '<span class="nb-lean push">≈ 貼盤</span>'
        elif diff > 0:
            lean = f'<span class="nb-lean over">偏大 +{diff:.1f}</span>'
            pick = True
        else:
            lean = f'<span class="nb-lean under">偏小 {diff:.1f}</span>'
            pick = False
        lineblk = f'<div class="line">盤口總分 <b>{float(tl):.1f}</b>　·　<b>{esc(src)}</b></div>'
        hero = (f'<div class="nb-totline"><span class="led big">{pt:.0f}</span>'
                f'<span class="vs">模型估總分</span>{lean}</div>')
    else:
        lineblk = f'<div class="line">無總分盤口可比較　·　<b>{esc(src)}</b></div>'
        hero = (f'<div class="nb-totline"><span class="led big">{pt:.0f}</span>'
                f'<span class="vs">模型估總分</span></div>')

    res = ""
    if g["status"] == "final" and pd.notna(g.get("over_result")):
        orr = int(g["over_result"])
        if orr == 2:
            res = '<div class="res"><span class="score">和盤 push</span><span>退回</span></div>'
        elif pick is not None:
            went_over = orr == 1
            correct = went_over == pick
            actual = "大" if went_over else "小"
            badge = ('<span class="nb-hit">命中</span>' if correct
                     else '<span class="nb-miss">未中</span>')
            res = f'<div class="res"><span class="score">開出 {actual}分</span>{badge}</div>'
    return f'{head}{lineblk}{hero}{caveat}{res}</div>'


def injury_panel(df: pd.DataFrame, team_abbr: str, who: str) -> str:
    if df is None or df.empty:
        return (f'<div class="nb-panel"><h3>{fmt_team(team_abbr)} '
                f'<span>{who}隊傷病</span></h3><div class="empty">沒有傷病回報</div></div>')
    snap = df.iloc[0]["snap_date"]
    snap_lbl = (pd.Timestamp(snap).strftime("%m/%d 回報")
                if snap is not None and not pd.isna(snap) else "")
    rows = []
    for _, r in df.iterrows():
        raw = str(r["status"] or "")
        cls = "out" if "out" in raw.lower() else "dtd"
        stt = tw_status(raw)
        det = tw_detail(r["detail"])
        rows.append(
            f'<div class="nb-inj"><span class="pn">{esc(r["player_name"])}</span>'
            f'<span class="nb-injstatus {cls}">{esc(stt)}</span>'
            f'<span class="det">{esc(det)}</span></div>'
        )
    return (f'<div class="nb-panel"><h3>{fmt_team(team_abbr)} '
            f'<span>{who}隊傷病 {snap_lbl}</span></h3>{"".join(rows)}</div>')


# =========================================================
# Page assembly (each returned chunk is one markdown block)
# =========================================================

EMPTY_BOARD_TEXT = "目前資料庫尚無預測。賽季開始後會自動更新每日預測。"


def not_found_chunks(acc, links: Links = STREAMLIT_LINKS) -> list[str]:
    return [topbar_html(acc, back=True, links=links),
            '<div class="nb-honest">查無此場比賽。</div>']


def detail_chunks(g: pd.Series, tr: dict, inj_away: pd.DataFrame, inj_home: pd.DataFrame,
                  players_away: pd.DataFrame, players_home: pd.DataFrame, acc,
                  links: Links = STREAMLIT_LINKS) -> list[str]:
    home, away = g["home_abbr"], g["away_abbr"]
    season = g["season"]
    gdate = g["game_date_et"]
    final = g["status"] == "final"

    score = ""
    if final and pd.notna(g["home_score"]):
        score = f'，{esc(away)} {int(g["away_score"])} : {int(g["home_score"])} {esc(home)}'
    return [
        topbar_html(acc, back=True, links=links),
        f'<div class="nb-detailhead">{matchup_block(g, big=True)}{prob_bar(g, big=True)}'
        f'<div class="when">{fmt_when(g["tipoff_utc"], gdate)}（台北時間），'
        f'{"終場" if final else "尚未開賽"}{score}</div></div>',
        '<div class="nb-sechead" style="margin-top:30px;"><h2>玩法預測</h2>'
        '<span class="sub">勝負、受讓分、大小分</span></div>',
        f'<div class="nb-bets">{_bet_moneyline(g)}{_bet_spread(g)}'
        f'{_bet_total(g, (tr or {}).get("over_wf_acc"))}</div>',
        '<div class="nb-sechead" style="margin-top:36px;"><h2>傷病名單</h2>'
        '<span class="sub">最新回報（≤ 該場日期）</span></div>',
        f'<div class="nb-twocol">{injury_panel(inj_away, away, "客")}'
        f'{injury_panel(inj_home, home, "主")}</div>',
        f'<div class="nb-sechead" style="margin-top:36px;"><h2>球員場均</h2>'
        f'<span class="sub">{esc(season)} 球季，上場 10 分鐘以上</span></div>',
        f'<div class="nb-twocol">'
        f'<div class="nb-panel"><h3>{fmt_team(away)} <span>客隊</span></h3>{table_html(players_away)}</div>'
        f'<div class="nb-panel"><h3>{fmt_team(home)} <span>主隊</span></h3>{table_html(players_home)}</div>'
        f'</div>',
        DISCLAIMER_HTML,
    ]


def hero_html(n_up: int, now: dt.datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(TW)
    if n_up:
        headline = f"今晚 {n_up} 場"
        sub = (f"{local:%m/%d}，台北時間。模型用球隊近況與實力逐場算出勝率，每場只用開賽前的資料。"
               "勝率不是推薦：盤口通常比模型準。")
    else:
        headline = "休賽季：近期預測與結果"
        sub = ("目前 NBA 休賽季、暫無賽事。以下回顧近期比賽的預測與實際結果，"
               "以及模型的長期戰績。賽季開始後會自動恢復每日預測。")
    return f'<div class="nb-hero"><h1>{headline}</h1><p class="nb-lede">{sub}</p></div>'


def record_chunks(tr: dict, acc) -> list[str]:
    acc_cell = (f'<div class="nb-rcell"><div class="k">時間外驗證猜中率</div>'
                f'<div class="v c led">{acc * 100:.1f}%</div>'
                f'<div class="note">{esc(tr.get("wf_n"))} 場時間外驗證</div></div>') if acc else \
               ('<div class="nb-rcell"><div class="k">時間外驗證猜中率</div>'
                '<div class="v led">—</div><div class="note">尚未訓練</div></div>')
    if tr["rec_n"]:
        hit_pct = tr["rec_hit"] / tr["rec_n"] * 100
        rec_cells = (
            f'<div class="nb-rcell"><div class="k">已結算預測</div><div class="v led">{tr["rec_n"]}</div>'
            f'<div class="note">上線以來實際場次</div></div>'
            f'<div class="nb-rcell"><div class="k">其中命中</div><div class="v m led">{hit_pct:.1f}%</div>'
            f'<div class="note">{tr["rec_hit"]} / {tr["rec_n"]} 場</div></div>'
        )
    else:
        rec_cells = (
            '<div class="nb-rcell"><div class="k">已結算預測</div><div class="v led">0</div>'
            '<div class="note">尚無已結算場次</div></div>'
            '<div class="nb-rcell"><div class="k">其中命中</div><div class="v led">—</div>'
            '<div class="note">等待結算</div></div>'
        )
    over_acc = tr.get("over_wf_acc")
    over_cells = ""
    if over_acc:
        over_cells += (f'<div class="nb-rcell"><div class="k">大小分時間外猜中率</div>'
                       f'<div class="v m led">{over_acc * 100:.1f}%</div>'
                       f'<div class="note">{esc(tr.get("over_wf_n"))} 場 O/U 驗證</div></div>')
    if tr.get("over_rec_n"):
        op = tr["over_rec_hit"] / tr["over_rec_n"] * 100
        over_cells += (f'<div class="nb-rcell"><div class="k">大小分已結算命中</div>'
                       f'<div class="v led">{op:.1f}%</div>'
                       f'<div class="note">{tr["over_rec_hit"]} / {tr["over_rec_n"]} 場</div></div>')
    return [
        '<div class="nb-sechead" style="margin-top:24px;"><h2>模型戰績</h2>'
        '<span class="sub">每場只用開賽前的資料驗證</span></div>',
        f'<div class="nb-record">{acc_cell}{rec_cells}{over_cells}</div>',
        '<div class="nb-honest">「猜中率」是每場只用該場之前的資料、不偷看未來算出來的，所以誠實可信；'
        '模型越有把握，命中率越高。<b>但這不等於能賺錢</b>——運彩賠率早已反映強弱，高命中率 ≠ 能獲利。'
        '本站是數據預測與分析，不是穩賺明牌。</div>',
    ]


def _pick_dates(games: pd.DataFrame) -> tuple[list, list]:
    """(shown, folded) game dates: the first date that still has unplayed games ("tonight") and the
    latest date before it whose games are all final; with nothing upcoming, the two latest dates."""
    by_date = {d: grp for d, grp in games.groupby("game_date_et", sort=False)}
    dates = sorted(by_date)
    pending = [d for d in dates if (by_date[d]["status"] != "final").any()]
    if pending:
        tonight = pending[0]
        done = [d for d in dates if d < tonight and (by_date[d]["status"] == "final").all()]
        shown = [tonight] + done[-1:]
    else:
        shown = list(reversed(dates))[:2]
    folded = [d for d in reversed(dates) if d not in shown]
    return shown, folded


def board_chunks(games: pd.DataFrame, tr: dict, acc, now: dt.datetime,
                 links: Links = STREAMLIT_LINKS) -> list[str]:
    """Top bar and headline, then tonight's games, the last finished day, other dates folded, the record."""
    if games.empty:
        return [topbar_html(acc, links=links), hero_html(0, now)]
    shown, folded = _pick_dates(games)
    by_date = {d: grp for d, grp in games.groupby("game_date_et", sort=False)}
    tonight = by_date[shown[0]]
    n_up = int((tonight["status"] != "final").sum())
    chunks = [topbar_html(acc, links=links), hero_html(n_up, now)]
    for d in shown:
        chunks.append(date_group_html(d, [r for _, r in by_date[d].iterrows()], links))
    if folded:
        n_more = sum(len(by_date[d]) for d in folded)
        inner = "".join(date_group_html(d, [r for _, r in by_date[d].iterrows()], links) for d in folded)
        chunks.append(f'<details class="nb-more"><summary>其他日期（{n_more} 場）</summary>{inner}</details>')
    chunks.extend(record_chunks(tr, acc))
    return chunks


def team_section_head(season: str, with_game_hint: bool = True) -> str:
    hint = "，點上方對戰可看雙隊對比" if with_game_hint else ""
    return (f'<div class="nb-sechead" style="margin-top:36px;"><h2>球隊數據</h2>'
            f'<span class="sub">{esc(season)} 球季{hint}</span></div>')


def team_tables_html(team: str, team_df: pd.DataFrame, player_df: pd.DataFrame) -> str:
    return (f'<div class="nb-tables">'
            f'<div class="nb-panel"><h3>{TEAM_NAME_CH[team]} 近 10 場 <span>主＝主場</span></h3>'
            f'{table_html(team_df)}</div>'
            f'<div class="nb-panel"><h3>{TEAM_NAME_CH[team]} 球員場均 <span>min ≥ 10</span></h3>'
            f'{table_html(player_df)}</div>'
            f'</div>')


def explainer_md(acc) -> str:
    return f"""
- **資料**：ESPN 的球隊與球員數據，2015-16 球季起。
- **模型**：用球隊近況、Elo 實力、四要素、休息天數等預測雙方分差，
  再換算成勝率。每場只用開賽前的資料，所以驗證數字不灌水。
- **誠實話**：時間外驗證猜中勝負約 {acc * 100:.0f}%，但盤口比模型準，這不等於能在運彩賺錢。
  本站定位是**數據預測與分析**，不是穩賺明牌。
""" if acc else """
- **資料**：ESPN 的球隊與球員數據，2015-16 球季起。
- **模型**：用球隊近況、Elo 實力、四要素、休息天數等預測雙方分差，
  再換算成勝率。每場只用開賽前的資料，所以驗證數字不灌水。
- **誠實話**：盤口比模型準，猜中勝負不等於能在運彩賺錢。本站定位是**數據預測與分析**，不是穩賺明牌。
"""


# =========================================================
# Static site shell
# =========================================================

STALE_HOURS = 36
NOT_YET_HOURS = 20           # newest prediction older than this ...
NEXT_GAME_WINDOW_HOURS = 24  # ... while a game tips off within this => "not out yet"


def should_show_not_yet(now: dt.datetime, data_at: dt.datetime | None,
                        next_tip: dt.datetime | None) -> bool:
    """Reference for the page's inline JS (keep both in step): today's picks are
    probably missing when a game is coming up soon but the newest prediction is old."""
    if next_tip is None or data_at is None:
        return False
    return (now < next_tip
            and next_tip - now < dt.timedelta(hours=NEXT_GAME_WINDOW_HOURS)
            and now - data_at > dt.timedelta(hours=NOT_YET_HOURS))


class StaticLinks:
    """Relative links for the static site; ``depth`` 0 = site root, 1 = game/ or team/."""

    def __init__(self, depth: int):
        self._up = "../" * depth

    def home(self) -> str:
        return f"{self._up}index.html"

    def game(self, game_id: str) -> str:
        return f"{self._up}game/{game_id}.html"

    def team(self, abbr: str) -> str:
        return f"{self._up}team/{abbr}.html"


STATIC_EXTRA_CSS = """
<style>
html, body { margin: 0; background: var(--ground); }
body { color: var(--text); font-family: var(--body); }
.stApp { min-height: 100vh; }
.stApp .block-container { box-sizing: border-box; width: 100%; margin: 0 auto;
  padding-left: 16px; padding-right: 16px; }
.stApp .block-container > * + * { margin-top: 1rem; }
.nb-stale { margin: 0 0 12px; padding: 8px 12px; border-left: 3px solid var(--miss);
  color: var(--text); background: var(--surface); }
.nb-updated { margin: 24px 0 6px; color: var(--muted); font-size: 13px; }
.nb-teamlist { list-style: none; margin: 0; padding: 0; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px 14px; }
.nb-teamlist a, .nb-static-link { color: var(--text); }
.nb-teamlist a { display: inline-block; padding: 6px 0; }
details.nb-explainer { margin: 16px 0; padding: 10px 14px; border: 1px solid var(--hair);
  border-radius: 10px; background: var(--surface); }
details.nb-explainer summary { cursor: pointer; color: var(--muted); }
/* dark: follows the system setting; data-theme forces one (used by scripts/preview_site.py) */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0F131A; --surface: #171C24; --text: #E6EAF0; --muted: #9AA4B2; --faint: #9AA4B2;
    --hair: #29313C; --hair-strong: #3A4350; --fill: #C9D2DE; --cyan: #E6EAF0; --magenta: #E6EAF0; --violet: #E6EAF0;
    --accent-soft: #29313C; --win: #5CC48F; --miss: #F07A6E; --amber: #E6EAF0;
  }
}
:root[data-theme="dark"] {
  --ground: #0F131A; --surface: #171C24; --text: #E6EAF0; --muted: #9AA4B2; --faint: #9AA4B2;
    --hair: #29313C; --hair-strong: #3A4350; --fill: #C9D2DE; --cyan: #E6EAF0; --magenta: #E6EAF0; --violet: #E6EAF0;
    --accent-soft: #29313C; --win: #5CC48F; --miss: #F07A6E; --amber: #E6EAF0;
}
</style>
"""


def static_page(title: str, body_html: str, built_at: dt.datetime,
                data_at: dt.datetime | None = None,
                next_tip: dt.datetime | None = None) -> str:
    """Full HTML document for the static site.

    ``built_at`` is when the page was generated; ``data_at`` is the newest
    prediction time in the DB. Staleness is judged on ``data_at`` (falling back
    to ``built_at`` when unknown), so a rebuild without fresh data still warns.
    Both must be timezone-aware.
    """
    if built_at.tzinfo is None:
        raise ValueError("built_at must be timezone-aware")
    if data_at is not None and data_at.tzinfo is None:
        raise ValueError("data_at must be timezone-aware")
    if next_tip is not None and next_tip.tzinfo is None:
        raise ValueError("next_tip must be timezone-aware")
    stamp = data_at or built_at
    built_iso = stamp.isoformat(timespec="seconds")
    built_txt = built_at.astimezone(TW).strftime("%Y-%m-%d %H:%M")
    if data_at is not None:
        data_txt = data_at.astimezone(TW).strftime("%Y-%m-%d %H:%M")
        shown = f"資料更新於 {data_txt}（台北時間），頁面建置於 {built_txt}（台北時間）"
    else:
        shown = f"頁面建置於 {built_txt}（台北時間）；沒有預測資料時間可顯示"
    tip_attr = (f' data-next-tip="{esc(next_tip.isoformat(timespec="seconds"))}"'
                if next_tip is not None else "")
    notyet = ""
    if data_at is not None:
        notyet = ('<div id="notyet" class="nb-stale" hidden>今天的預測還沒出來，以下是 '
                  f'{data_at.astimezone(TW):%m/%d} 的預測。</div>')
    return (
        '<!doctype html>\n<html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>{NEON_CSS}{STATIC_EXTRA_CSS}</head><body>'
        '<div class="stApp"><div class="block-container">'
        '<div id="stale" class="nb-stale" hidden>⚠ 資料可能已過期：'
        f'超過 {STALE_HOURS} 小時沒有更新，以下內容可能不是最新。</div>'
        f'{notyet}'
        f'{body_html}'
        f'<div class="nb-updated" id="built" data-built="{esc(built_iso)}"{tip_attr}>'
        f'{shown}</div>'
        '</div></div>'
        '<script>(function(){var el=document.getElementById("built");'
        'var t=Date.parse(el.getAttribute("data-built"));'
        f'var STALE_MS={STALE_HOURS}*3600*1000;'
        'if(!isNaN(t)&&Date.now()-t>STALE_MS){'
        'document.getElementById("stale").hidden=false;}'
        'var nt=Date.parse(el.getAttribute("data-next-tip")||""),ny=document.getElementById("notyet"),'
        'now=Date.now();'
        f'if(ny&&!isNaN(t)&&!isNaN(nt)&&now<nt&&nt-now<{NEXT_GAME_WINDOW_HOURS}*3600*1000'
        f'&&now-t>{NOT_YET_HOURS}*3600*1000){{ny.hidden=false;}}'
        '})();</script>'
        '</body></html>\n'
    )
