# 靜態網站（一直在線）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把現有 Streamlit 看板抽成共用渲染模組，另產生可放在 GitHub Pages 的靜態網站（與 Streamlit 版對等），並用 workflow 在每次排程後重建。

**Architecture:** `webui/render.py` 放純渲染函式（資料列轉 HTML 字串，不 import streamlit、不查 DB），`webui/queries.py` 放只含 SELECT 的查詢（接收 `run(sql, params) -> DataFrame`），`webui/build_site.py` 讀 DB、渲染、輸出 `site/`。`auto_nba_predict.py` 變成 Streamlit 外殼，輸出必須與改前逐字相同（整頁 golden 守住）。

**Tech Stack:** Python 3.11、pandas 2.3.3、streamlit 1.54.0（`streamlit.testing.v1.AppTest`）、pytest、psycopg2、GitHub Actions + Pages。不新增任何依賴（`requirements.txt` 不動；PyYAML 不在其中，測試不得 import yaml）。

**Spec:** `docs/superpowers/specs/2026-10-21-static-site-design.md`（已納入支線審查意見）。

## Global Constraints

- 時間：DB 存 UTC；頁面顯示台北時間（`Asia/Taipei`）；比賽日用 ET。
- 全站連結一律相對路徑，不得有以 `/` 開頭的 `href`/`src`（網址帶 `/nba-predict/` 子路徑）。
- 過期門檻 36 小時；每頁顯示「資料更新於 YYYY-MM-DD HH:MM（台北時間）」。
- `webui/queries.py` 只含 SELECT；`build_site` 連線 `set_session(readonly=True)`；不改 `jobs.db_utils.db_connect`。
- DB 來的文字輸出前一律 `esc()`（`html.escape(str(x), quote=True)`）；`NEON_CSS`、`DISCLAIMER_HTML` 與其他模板不 escape。
- 隊名只走 `jobs/teams.py`：`require_abbr`、`TEAM_NAME_CH`。未知縮寫要報錯，不補零。
- `site.yml`：頂層 `permissions: contents: read`；`pages: write`、`id-token: write` 只給 `deploy`；DB secrets 只放 `build_site` 那一個 step 的 `env`；`checkout` 不指定 ref；`build` job `timeout-minutes: 15`；併發群組 `pages`、`cancel-in-progress: false`。
- 不讀、不印、不 commit `.streamlit/secrets.toml`。不跑會寫 DB 的 job。**push、啟用 Pages、觸發 workflow 都要使用者明確同意**（Task 9）。
- commit 訊息英文，結尾加 `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`。不動已追蹤的 `tests/__pycache__/*.pyc`。
- 每個 task 結束前跑 `python -m pytest tests -q`，貼出實際輸出。

## Review Focus

1. 比賽清單中 `game_id` 含 `/`、`..` 或空白 → build 必須失敗，不寫出檔案（Task 6）。
2. DB 完全沒資料（`v_app_board` 空）→ build 成功，index 顯示「尚無預測」，所有連結有效（Task 6）。
3. `v_app_board` 出現不在 `TEAM_NAME_CH` 的縮寫 → build 失敗並指出縮寫，不默默顯示（Task 6）。
4. 某隊沒有任何比賽或球員資料 → 該隊頁顯示「尚無資料」，30 頁仍都產生（Task 6）。
5. 球員名／傷病說明含 `<script>`、`'`、`&` → 輸出被轉義，頁面不出現可執行標籤（Task 4）。

---

### Task 1: 假 DB 與整頁特徵化 golden（先於任何搬動）

**Files:**
- Create: `tests/fakes.py`、`tests/test_streamlit_golden.py`、`tests/golden/*.txt`（由測試產生）
- Test: `tests/test_streamlit_golden.py`

**Interfaces:**
- Produces: `tests/fakes.py` 的 `class FakeDb`：`FakeDb(routes: dict[str, pd.DataFrame | Callable[[tuple], pd.DataFrame]])`；`.run(sql: str, params: tuple = ()) -> pd.DataFrame`（SQL 空白正規化後，依 key 子字串比對；找不到任何 key 就 `raise AssertionError(sql)`）；`.connect(*a, **kw)` 回傳可當 `with conn, conn.cursor() as cur` 用、`execute/fetchall/description` 行為的假連線，內部走 `.run`。
- Produces: `tests/fakes.py` 的情境函式 `scenario_board() -> FakeDb`、`scenario_offseason() -> FakeDb`、`scenario_empty() -> FakeDb`，以及常數 `GAME_UPCOMING = "0022600001"`、`GAME_FINAL = "0022600002"`、`GAME_NOLINE = "0022600003"`、`GAME_MISSING = "9999999999"`。後續 task 的 build 測試重用它們（只用 `.run`）。

- [ ] **Step 1: 寫 `tests/fakes.py`。** 路由 key（SQL 正規化後的子字串）：`"FROM public.v_app_board WHERE p_home_win IS NOT NULL"`（最新場次）、`"FROM public.v_app_board WHERE game_id"`、`"FROM public.model_registry_v2"`（依 `params[0]` 為 `MODEL_NAME` 或 `"total_model"` 回不同 metrics）、`"(p_home_win>=0.5)"`、`"(p_over>=0.5)"`、`"FROM public.team_game_stats"`、`"FROM public.player_game_stats"`、`"SELECT season FROM public.games_v2"`、`"FROM public.injury_snapshots"`。欄位名須與 `auto_nba_predict.py:72-186` 的 SELECT 一致。`scenario_board`：三場——`GAME_UPCOMING`（status `scheduled`，有 `home_spread`、`p_home_cover`、`total_line`、`pred_total`、`line_source`）、`GAME_FINAL`（status `final`，`win_result`/`cover_result`/`over_result` 皆有值）、`GAME_NOLINE`（final、無盤口、`pred_total` 為 None）；`tipoff_utc` 一律帶 UTC 時區；球員／球隊名只用 ASCII 字母與一般中文，傷病含 `Out` + `Knee Surgery` 與 `Day-To-Day` + `Not Specified`。`scenario_offseason`：只有兩場 final。`scenario_empty`：所有查詢回空 DataFrame（欄位名仍要在）。

- [ ] **Step 2: 寫 `tests/test_streamlit_golden.py`。** 用 `AppTest.from_file("auto_nba_predict.py", default_timeout=30)`；fixture 內 `monkeypatch.setenv("DATABASE_URL", "postgresql://fake")`、`monkeypatch.setattr(psycopg2, "connect", db.connect)`、`st.cache_data.clear()`。函式 `render_page(db, query_params: dict) -> str`：回傳 `[m.value for m in at.markdown]` 與 `[i.value for i in at.info]` 依序以 `"\n<!--chunk-->\n"` 串起來；並把 kicker 的日期正規化（regex `\d{4}\.\d{2}\.\d{2}` → `DATE`）。測試：`test_golden_board`、`test_golden_detail_upcoming`（`query_params={"game": GAME_UPCOMING}`）、`test_golden_detail_final`、`test_golden_detail_noline`、`test_golden_detail_missing`（`GAME_MISSING`）、`test_golden_offseason`、`test_golden_empty`。每個比對 `tests/golden/<name>.txt`；環境變數 `UPDATE_GOLDEN=1` 時改為寫檔。`at.run()` 後先 `assert not at.exception`。若 `at.query_params` 在 1.54 不能這樣設，改用 `at.query_params = {...}`，並在測試註解寫明。

- [ ] **Step 3: 產生 golden，並人工抽看。** Run: `UPDATE_GOLDEN=1 python -m pytest tests/test_streamlit_golden.py -q`；再 Run: `python -m pytest tests -q`。Expected: 全部 PASS（原 26 個＋新 7 個）。打開 `tests/golden/board.txt` 確認有卡片、`模型戰績`、選單區塊的 HTML；`detail_upcoming.txt` 有三種玩法卡與傷病面板。

- [ ] **Step 4: 驗證 golden 真的抓得到改動。** 暫時把 `auto_nba_predict.py:623` 的「看好」改成「看壞」，Run 測試 Expected: `test_golden_board` FAIL；還原後 Expected: PASS。貼出兩次輸出。

- [ ] **Step 5: Commit** `git add tests/fakes.py tests/test_streamlit_golden.py tests/golden && git commit -m "Add whole-page golden tests for the Streamlit app"`

---

### Task 2: 抽出 `webui/render.py`（純搬動，golden 逐字相同）

**Files:**
- Create: `webui/__init__.py`（空）、`webui/render.py`、`tests/test_webui_render.py`
- Modify: `auto_nba_predict.py`（`:193-893` 的渲染程式搬走，`:900-1044` 改呼叫 render）
- Test: `tests/test_streamlit_golden.py`（不得改動 golden）、`tests/test_webui_render.py`

**Interfaces:**
- Produces（`webui/render.py`，皆不 import streamlit、不呼叫 `datetime.now`、不查 DB）：
  - `class Links(Protocol)`: `home() -> str`、`game(game_id: str) -> str`、`team(abbr: str) -> str`；`class StreamlitLinks`（`home()="?"`、`game(i)=f"?game={i}"`、`team(a)="?"`）；`STREAMLIT_LINKS = StreamlitLinks()`。
  - 原樣搬移（名稱、行為不變）：`NEON_CSS`、`DISCLAIMER_HTML`、`INJ_STATUS_CH`、`INJ_TERM_CH`、`fmt_team`、`tw_status`、`tw_detail`、`fmt_when`、`matchup_block`、`prob_bar`、`featured_html`、`table_html`、`_bet_moneyline`、`_bet_spread`、`_bet_total`、`_conf_word`、`_fill_html`。
  - 加 `links: Links = STREAMLIT_LINKS` 參數：`game_card_html(g, links=...)`（`href` 用 `links.game(g["game_id"])`）、`date_group_html(date_val, rows, links=...)`、`topbar_html(acc, back=False, links=...)`（返回連結用 `links.home()`）。
  - `injury_panel(df: pd.DataFrame, team_abbr: str, who: str) -> str`（改為接收已查好的傷病表）。
  - `detail_chunks(g: pd.Series, tr: dict, inj_away: pd.DataFrame, inj_home: pd.DataFrame, players_away: pd.DataFrame, players_home: pd.DataFrame, acc, links=...) -> list[str]`：依序回傳 `render_detail` 現在每次 `st.markdown` 的內容（topbar、detailhead、玩法標題、玩法卡、傷病標題、傷病面板、球員標題、球員表、`DISCLAIMER_HTML`），共 9 塊。
  - `not_found_chunks(acc, links=...) -> list[str]`（topbar + `查無此場比賽。` 區塊）。
  - `board_chunks(games: pd.DataFrame, tr: dict, acc, now: datetime, links=...) -> list[str]`：topbar、hero，之後若 `games` 非空：featured（僅當有未開賽）、預測標題、每個日期群組一塊、戰績標題、戰績格、誠實聲明；`games` 為空時只回前兩塊。`now` 必須是帶時區的 `datetime`，kicker 用 `now.astimezone(TW)`。
  - `EMPTY_BOARD_TEXT = "目前資料庫尚無預測。賽季開始後會自動更新每日預測。"`
  - `team_section_head(season: str) -> str`、`team_tables_html(team: str, team_df, player_df) -> str`、`explainer_md(acc) -> str`（就是現有 expander 內的兩種 markdown）。
- Produces: `auto_nba_predict.py` 保留 `q`、`pg_conn`、`load_*`（仍呼叫原 SQL，Task 3 才搬）；頁面改為「呼叫上述函式，逐塊 `st.markdown(chunk, unsafe_allow_html=True)`」，Streamlit 專屬的 `st.info`、`st.selectbox`、`st.expander`、sidebar 留在原檔。`now` 以 `dt.datetime.now(TW)` 傳入。

- [ ] **Step 1: 先寫 render 單元測試（會失敗）** `tests/test_webui_render.py`，涵蓋規格 §8.2：`test_game_card_uses_links`（自訂 `Links` 讓 `href` 變成 `../game/X.html`）、`test_card_upcoming_and_final`、`test_card_missing_tipoff`（`tipoff_utc` 為 `None` / `pd.NaT` 時顯示 `game_date_et`）、`test_bet_total_variants`（無 `pred_total`+無盤口 → 含 `🔧 本季開賽後顯示`；有盤口無預測；有預測無盤口 → 含 `無總分盤口可比較`；差 < 0.5 → `貼盤`）、`test_bet_spread_missing_line`（含 `這場沒有盤口資料`）、`test_injury_panel_empty_df`（含 `（無傷病回報）`）、`test_board_chunks_empty_games_returns_two_chunks`、`test_board_chunks_only_final_has_no_featured`、`test_no_streamlit_import`（`webui/render.py` 與 `webui/queries.py` 原始碼不含 `import streamlit` 與 `from streamlit`）。

- [ ] **Step 2: Run** `python -m pytest tests/test_webui_render.py -q`。Expected: FAIL（`No module named 'webui'`）。

- [ ] **Step 3: 實作 `webui/render.py`**：照上面介面搬移；搬的時候不改任何字串。`board_chunks` 內戰績格邏輯照 `auto_nba_predict.py:956-999` 原樣放入，kicker 照 `:919-928`。

- [ ] **Step 4: 改 `auto_nba_predict.py`**：刪掉已搬走的函式與常數，頁面改呼叫 render（`import` 自 `webui.render`）；`render_detail` 縮成「載入資料 → `detail_chunks` → 逐塊輸出」。保留所有 `@st.cache_data(ttl=…)` 與 SQL 不動。

- [ ] **Step 5: Run** `python -m pytest tests -q`。Expected: 全 PASS，且 `git diff --stat tests/golden` 為空（golden 沒被改）。再執行 `streamlit run auto_nba_predict.py --server.headless true`：沒有 DB 連線就回報「未驗證」，有就貼啟動輸出與首頁是否載入。

- [ ] **Step 6: Commit** `git add webui tests/test_webui_render.py auto_nba_predict.py && git commit -m "Extract pure rendering into webui.render"`

---

### Task 3: 抽出 `webui/queries.py` 與唯讀測試

**Files:**
- Create: `webui/queries.py`、`tests/test_webui_queries.py`
- Modify: `auto_nba_predict.py:71-186`（`load_*` 變薄包裝）

**Interfaces:**
- Consumes: Task 1 的 `FakeDb`。
- Produces（`webui/queries.py`；`Run = Callable[[str, tuple], pd.DataFrame]`）：`recent_games(run: Run, n: int = 30)`、`track_record(run: Run) -> dict`（回傳 dict 鍵與 `load_track_record` 現況完全相同）、`team_recent(run, abbr, n=10)`、`player_recent(run, abbr, season)`、`latest_season(run) -> str`、`game(run, gid)`、`injuries(run, team_abbr, ref_date)`。SQL 字串逐字從 `auto_nba_predict.py` 搬過來。常數 `ALL_SQL: list[str]` 不需要；測試直接掃模組原始碼。
- Produces: `auto_nba_predict.py` 保留原名稱與原 TTL 的薄包裝，例如 `@st.cache_data(ttl=300) def load_recent_games(n: int = 30): return queries.recent_games(q, n)`。**不要把 `run` 傳進 `st.cache_data`。**

- [ ] **Step 1: 寫測試** `test_queries_are_read_only`：讀 `webui/queries.py` 原始碼，對 `re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b", re.I)` 掃所有字串常值（用 `ast` 取出 `ast.Constant` 的 str），不得有命中（`updated_at` 不算命中）。`test_track_record_shape`：用 `scenario_board()` 的 `.run` 呼叫 `queries.track_record`，鍵集合等於 `{"wf_acc","wf_n","rec_hit","rec_n","over_wf_acc","over_wf_n","over_rec_hit","over_rec_n"}`。`test_latest_season_fallback`：空 DB 回 `"2025-26"`。

- [ ] **Step 2: Run** `python -m pytest tests/test_webui_queries.py -q`。Expected: FAIL（模組不存在）。

- [ ] **Step 3: 實作並改 `auto_nba_predict.py`。** 照介面。

- [ ] **Step 4: Run** `python -m pytest tests -q`。Expected: 全 PASS，golden 未改（`git diff --stat tests/golden` 為空）。

- [ ] **Step 5: Commit** `git commit -am "Extract SQL into webui.queries"`（含新檔 `git add`）。

---

### Task 4: 全站 HTML escape（刻意的行為變更）

**Files:**
- Modify: `webui/render.py`
- Test: `tests/test_webui_render.py`、`tests/test_streamlit_golden.py`（golden 預期不變）

**Interfaces:**
- Produces: `webui/render.py` 的 `esc(x) -> str`（`html.escape(str(x), quote=True)`；`None`/NaN 視為空字串）。套用處：`player_name`、`tw_status`/`tw_detail` 的結果、`line_source`、`season`、所有 `abbr` 與 `fmt_team` 輸出、`game_id`（放進 `href` 時）。`table_html` 改為「複製 df，逐格 `esc`（只對字串／object 欄），再 `to_html(index=False, classes="ntable", border=0, escape=False)`」。欄位標題為程式內常數，不 escape。

- [ ] **Step 1: 寫測試（會失敗）**：`test_esc_special_chars`（`esc("D'Angelo")=="D&#x27;Angelo"`、`esc("<script>")` 無 `<`）、`test_injury_panel_escapes`（`player_name="<script>alert(1)</script>"`、`detail="Knee <b>"`，輸出不含 `<script>`、`<b>` 字樣）、`test_table_html_escapes_cells`（球員名 `A&B`、`C<D` → `A&amp;B`、`C&lt;D`，且標題 `球員` 原樣）、`test_card_escapes_game_id_in_href`、`test_team_name_with_apostrophe_not_double_escaped`（`esc(esc(x))` 不得被呼叫兩次：以 `"&amp;amp;" not in html` 斷言）。

- [ ] **Step 2: Run** `python -m pytest tests/test_webui_render.py -q`。Expected: 新測試 FAIL。

- [ ] **Step 3: 實作。** 注意 `tw_detail` 對未翻譯詞原樣輸出，escape 要在翻譯之後。

- [ ] **Step 4: Run** `python -m pytest tests -q`。Expected: 全 PASS。Task 1 fixture 只含一般字元，所以 golden 不應變動——`git diff --stat tests/golden` 若有輸出，表示 escape 改到了一般文字，必須查原因，不准直接更新 golden。

- [ ] **Step 5: Commit** `git commit -am "Escape DB-sourced text in rendered HTML"`。commit 內文註明：這是刻意的行為變更，Streamlit 版輸出對特殊字元會不同。

---

### Task 5: 靜態頁外殼與過期提示

**Files:**
- Modify: `webui/render.py`
- Test: `tests/test_webui_render.py`

**Interfaces:**
- Produces: `STALE_HOURS = 36`；`class StaticLinks`（實作 `Links`；`StaticLinks(depth: int)`，`depth=0` 為 `index.html`，`depth=1` 為 `game/…`、`team/…`；`home()` → `"index.html"` 或 `"../index.html"`，`game(i)` → `"game/<i>.html"` 或 `"../game/<i>.html"`，`team(a)` → 同理 `team/<a>.html`）；`static_page(title: str, body_html: str, built_at: datetime) -> str`：完整 `<!doctype html>` 文件，含 `<meta charset>`、viewport、`NEON_CSS`、`STATIC_EXTRA_CSS`（body 邊距與 `<details>` 樣式）、`<div class="stApp"><div class="block-container">…</div></div>`、更新時間文字（台北時間，格式 `資料更新於 YYYY-MM-DD HH:MM（台北時間）`）、`<div id="stale" hidden>…資料可能已過期…</div>`、以及帶 `data-built="<ISO8601 含時區>"` 的元素與 inline JS（`const STALE_MS = 36*3600*1000;`，超過就移除 `hidden`）。`built_at` 必須帶時區，否則 `raise ValueError`。

- [ ] **Step 1: 寫測試**：`test_static_links_depth0_and_1`、`test_static_links_no_leading_slash`、`test_static_page_has_built_marker`（斷言含 `data-built="2026-10-23T01:00:00+00:00"`）、`test_static_page_shows_taipei_time`（UTC 01:00 → `2026-10-23 09:00`）、`test_stale_threshold_is_36h`（原始碼含 `36*3600*1000`，且 `STALE_HOURS == 36`）、`test_static_page_rejects_naive_datetime`。

- [ ] **Step 2: Run** → FAIL。**Step 3: 實作。** **Step 4: Run** `python -m pytest tests -q` → PASS。

- [ ] **Step 5: Commit** `git commit -am "Add static page shell and staleness notice"`

---

### Task 6: `build_site.py`（頁面、連結檢查、原子替換）

**Files:**
- Create: `webui/build_site.py`、`tests/test_webui_build.py`
- Modify: `.gitignore`（加 `site/`）

**Interfaces:**
- Consumes: Task 1 的 `FakeDb`；Task 3 的 `queries.*`；Task 2/5 的 `render.*`。
- Produces（`webui/build_site.py`）：
  - `safe_game_id(gid: str) -> str`：不符 `re.fullmatch(r"[0-9A-Za-z_-]+", gid)` 就 `raise ValueError`。
  - `build(run: Run, out_dir: Path, now: datetime) -> None`：先寫到 `out_dir.parent / (out_dir.name + ".tmp")`，`check_site` 通過才換成 `out_dir`（失敗時 `out_dir` 保持原樣、tmp 清掉）。產生 `index.html`、`game/<id>.html`（`recent_games(run, 30)` 的每一場）、`team/<ABBR>.html`（`TEAM_NAME_CH` 全部 30 隊）、`404.html`、`.nojekyll`。`index.html` 用 `board_chunks`（`StaticLinks(0)`）＋ 30 隊連結清單（取代下拉選單）＋ team 區塊改成連結＋ `<details><summary>ℹ️ 這個模型怎麼運作？</summary>`（內容用 `explainer_md` 轉成簡單 HTML：每行 `- **x**：y` 變 `<li><b>x</b>：y</li>`）＋ `DISCLAIMER_HTML`；`games` 為空時用 `<div class="nb-honest">EMPTY_BOARD_TEXT</div>`。所有 board 內 abbr 先過 `require_abbr`；未知縮寫讓 build 失敗。球隊頁無資料顯示 `table_html` 的「尚無資料」。
  - `check_site(root: Path) -> list[str]`：回傳問題清單（無 `index.html`；任何 `href`/`src` 以 `/` 開頭；任何相對 `href` 指到不存在的檔案；忽略 `http(s):`、`#`、`mailto:`）。

- [ ] **Step 1: 寫測試** `tests/test_webui_build.py`（用 `tmp_path` 與 `scenario_board().run`、固定 `now=datetime(2026,10,23,1,0,tzinfo=timezone.utc)`）：`test_build_creates_all_pages`（`index.html`、3 個 game 頁、30 個 team 頁、`404.html`、`.nojekyll`）、`test_build_links_all_resolve`（`check_site` 回 `[]`）、`test_no_absolute_paths`、`test_build_empty_db`（`scenario_empty()`：成功、`index.html` 含 `目前資料庫尚無預測`、30 隊頁仍在）、`test_team_without_data_shows_empty`（某隊頁含 `尚無資料`）、`test_unsafe_game_id_fails_and_keeps_old_site`（假 DB 的 `game_id="../x"`：`pytest.raises(ValueError)`；`out_dir` 事先放一個 `marker.txt`，失敗後它還在、且無 `.tmp` 殘留）、`test_unknown_abbr_fails`（board 含 `ZZZ` → 例外訊息含 `ZZZ`）、`test_check_site_detects_broken_link`（手寫一個含 `href="missing.html"` 的資料夾）、`test_game_page_escapes_player_name`、`test_staleness_marker_in_every_page`。

- [ ] **Step 2: Run** → FAIL。**Step 3: 實作。** 單場頁用 `queries.game(run, gid)`、雙方 `queries.injuries(run, abbr, gdate)`、`queries.player_recent(run, abbr, season)` 餵 `detail_chunks`；`StaticLinks(1)`。

- [ ] **Step 4: Run** `python -m pytest tests -q` → PASS。

- [ ] **Step 5: 本機實際點看。** 寫一個臨時腳本（放 scratchpad，不進 repo）用 `scenario_board().run` 呼叫 `build` 到一個 `nba-predict/` 子資料夾下，再 `python -m http.server`（在該子資料夾的上一層）並用瀏覽器開 `http://localhost:8000/nba-predict/`，點首頁卡片、單場頁、球隊頁、返回連結；貼出每頁是否正常。若無法開瀏覽器就明說「未驗證」。

- [ ] **Step 6: Commit** `git add .gitignore webui/build_site.py tests/test_webui_build.py && git commit -m "Add static site builder with link check and atomic swap"`

---

### Task 7: 連線強化與 CLI 入口

**Files:**
- Modify: `webui/build_site.py`
- Test: `tests/test_webui_build.py`

**Interfaces:**
- Produces: `connect_readonly()`：設 `os.environ.setdefault("PGCONNECT_TIMEOUT", "10")`，呼叫 `jobs.db_utils.db_connect()`，`conn.set_session(readonly=True)`，`cur.execute("SET statement_timeout = 30000")`，回傳 conn；`make_run(conn) -> Run`；`main(argv: list[str] | None = None) -> int`：參數 `--out`（預設 `site`），用 `datetime.now(timezone.utc)` 當 `now`，成功回 0、任何例外印訊息（不得含連線字串或環境變數）後回 1。`if __name__ == "__main__": raise SystemExit(main())`。

- [ ] **Step 1: 寫測試**（用 monkeypatch 把 `jobs.db_utils.db_connect` 換成回傳假連線的函式；假連線記錄 `set_session` 與 `execute` 呼叫）：`test_connect_sets_readonly_and_timeout`、`test_main_returns_1_on_db_error_without_leaking_dsn`（讓 `db_connect` 拋 `RuntimeError("postgresql://u:secret@h/db")`，斷言 `capsys` 輸出不含 `secret`）、`test_main_builds_into_out_dir`。

- [ ] **Step 2: Run** → FAIL。**Step 3: 實作。** 例外訊息只輸出例外類別名稱與固定中文說明。**Step 4: Run** `python -m pytest tests -q` → PASS。

- [ ] **Step 5: Commit** `git commit -am "Harden build_site DB connection and add CLI"`

---

### Task 8: `.github/workflows/site.yml`

**Files:**
- Create: `.github/workflows/site.yml`、`tests/test_site_workflow.py`

**Interfaces:**
- Produces: workflow `site`。觸發：`workflow_run`（`workflows: ["daily_nba"]`、`types: [completed]`，不過濾 conclusion）、`workflow_dispatch`（boolean input `fail_build`，預設 false；為 true 時 `build` 第一個 step 就 `exit 1`，用來實測失敗不覆蓋線上）、`push` 到 `main` 且 `paths` 含 `webui/**`、`.github/workflows/site.yml`、`jobs/teams.py`、`jobs/model.py`、`jobs/db_utils.py`、`requirements.txt`、`tests/**`。頂層 `permissions: contents: read`、`concurrency: {group: pages, cancel-in-progress: false}`。`build`：`runs-on: ubuntu-latest`、`timeout-minutes: 15`；steps 依序 `actions/checkout`（不帶 `ref`）、`actions/setup-python`（3.11）、`pip install -r requirements.txt`、`python -m pytest tests -q`、名為 `Build site` 的 step（`run: python -m webui.build_site --out site`，`env` 帶 `DATABASE_URL`、`SUPABASE_HOST`、`SUPABASE_DB`、`SUPABASE_USER`、`SUPABASE_PASSWORD`、`SUPABASE_PORT` 六個 secrets）、`actions/upload-pages-artifact`（`path: site`）。`deploy`：`needs: build`、`permissions: {pages: write, id-token: write}`、`environment: {name: github-pages, url: ${{ steps.deployment.outputs.page_url }}}`、`actions/deploy-pages`。Action 版本在寫檔時先查官方文件確認（規格列 `upload-pages-artifact@v4`、`deploy-pages@v4`），查不到就回報，不憑記憶。

- [ ] **Step 1: 寫文字型測試**（不得 import yaml，CI 沒有 PyYAML）`tests/test_site_workflow.py`：讀檔，`test_secrets_only_in_build_site_step`（所有含 `secrets.` 的行必須落在 `- name: Build site` 之後、下一個 `- name:` 之前）、`test_no_ref_on_checkout`（檔案不含 `head_sha`、`head_branch`、`ref:`）、`test_least_privilege`（含頂層 `permissions:\n  contents: read`；`pages: write` 只出現一次且在 `deploy` 區塊之後）、`test_build_timeout`（含 `timeout-minutes: 15`）、`test_triggers`（含 `workflow_run`、`fail_build`、`jobs/teams.py`）。

- [ ] **Step 2: Run** → FAIL。**Step 3: 寫 workflow。** **Step 4: Run** `python -m pytest tests -q` → PASS。

- [ ] **Step 5: Commit** `git add .github/workflows/site.yml tests/test_site_workflow.py && git commit -m "Add site build and deploy workflow"`（尚未 push、尚未啟用 Pages）。

---

### Task 9: 文件、全面驗證、審查，然後停下來等使用者

**Files:**
- Modify: `CLAUDE.md`（架構加入 `webui/`、指令加入 `python -m webui.build_site`、不能違反的規則加「`webui/queries.py` 只准 SELECT」、已知待釐清加「`daily_nba` 停用中，靜態站內容會變舊」）、`docs/handoff/2026-10-22-static-site.md`（更新狀態）

- [ ] **Step 1: 更新文件並 commit**：`git commit -am "Document the static site in CLAUDE.md and handoff"`。
- [ ] **Step 2: 全面驗證，貼出實際輸出**：`python -m pytest tests -q`；`git diff --stat 9d5ccaf -- tests/golden` 確認 golden 只新增、沒有被改過；`python -m jobs.config` 不需要（沒動 config）；`streamlit run` 結果（沒有 DB 就寫「未驗證」）；列出 SQL／schema 沒變動。
- [ ] **Step 3: 交給 `reviewer` 子代理審整個 diff**（`git diff 9d5ccaf..HEAD`）。prompt 要重述：golden 必須逐字不變、DB 唯讀、secrets 只在 `Build site` step、不得放寬任何 gate、不可 push。把審查結果列給使用者，修完 Critical／High 再回報。
- [ ] **Step 4: 停下來，向使用者要明確同意後才做下列每一件事（逐項分別問）**：`git push`；把 repo 的 Pages 來源設為 GitHub Actions；手動觸發 `site` workflow；確認網址能開；用 `fail_build=true` 觸發一次，確認線上仍是上一版，並確認 `github-pages` environment 只允許預設分支；提醒使用者 `daily_nba` 停用期間約 36 小時後頁面會一直顯示「資料可能已過期」。
