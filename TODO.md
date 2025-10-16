# TODO / Roadmap（care_shift_test）

本文件說明目前系統現況、`shift_req.json` 規格、搜尋演算法設計，以及未來開發規劃與實作指引，之後可直接依此產生/修改程式碼。

## 目標與現況
- 目標：自週班表資料（SQLite）中，根據一或多個 case 的時段需求，找出所有可行的員工組合方案，排序後輸出，並支援自動/強制放寬策略以獲得更多候選。
- 已完成（重點檔案）：
  - `src/try_shift.py`：Stage 5 主流程，讀取 `shift_req.json`，產生候選解、排序輸出、支援 backup_strategy 與互動式放寬。
  - `src/orchestrator.py`：新增選單項目 `[5] try_shift` 呼叫 `run_try_shift()`。
  - `src/week_shift_import.py`：匯入週班表到 SQLite（`shifts` 表）。
  - `src/go_to_week_shift.py`：導覽與對話框處理。
  - `src/login.py`：登入（含圖形碼 OCR 與按鈕消失驗證、重試）。

## shift_req.json 規格（v1）
頂層欄位：
- `week_date`: `YYYY-MM-DD | null`，若為 null/空值以執行當週計算。
- `cases`: 陣列，每個元素為一個 case 物件。

case 欄位：
- `case_id` (string)：case 識別名。
- `week_date` (string|null, optional)：此 case 指定的週日期（優先於頂層）。
- `k` (int, optional)：允許的最大負責人數（預設 3；若任一日超過兩段需求，會自動放寬到 4）。
- `days` (object)：`sun|mon|tue|wed|thu|fri|sat` → `["HH:MM-HH:MM", ...]`。
- `specific` (array)：覆蓋特定日期，例如 `{ "date": "YYYY-MM-DD", "ranges": ["HH:MM-HH:MM", ...] }`。
- `backup_strategy` (object, optional)：目前支援
  - `{ "type": "relax_minutes", "minutes": 30 }`：即使 baseline 有解，也會「強制放寬」 minutes 再找一次候選並合併（列表會標記 `relax+30m`）。

未來可擴充欄位（提案）：
- `must_include`: 指定必須包含的員工名單。
- `must_exclude`: 指定不得包含的員工名單。
- `max_daily_hours`, `max_weekly_hours`: 個人上限限制。
- `tags_required`: 需求標籤（員工屬性匹配）。
- `step_relax`: `{ "minutes": 15, "steps": [0, 15, 30] }` 分階放寬。
- `widen_slot_min`: 臨時放大/縮小離散粒度 SLOT_MIN。

## 搜尋與排序（已實作）
- 離散化：以 `SLOT_MIN` 分鐘切時槽，做集合運算（聯集/交集/差集）。
- 貪婪挑選（多起點）：
  - 多個起點種子（依單人覆蓋潛力排序，含空起點），對每個種子執行貪婪選擇。
  - 平手決策：邊際覆蓋相同時，選「本週總工時較少」的員工。
  - 收集所有完成覆蓋的解，去重後輸出。
- 排序規則（候選解）：
  1) k（團隊人數）小→前面；2) 團隊總工時小→前面；3) 團隊成員字典序穩定化。
- 輸出：每個候選都列出成員及其覆蓋的連續時間區間；若 baseline 與 backup 皆無解，再進入互動式放寬。

## 待辦 / Roadmap
1) 參數化與 CLI/Orchestrator 整合
- `run_try_shift(db_path, req_path, week_date, ...)` 支援更多參數（最大種子數、最大輸出候選數、是否互動）。
- Orchestrator 第 5 步加入簡易問答設定（req 路徑、是否啟用 backup）。

2) 搜尋增強
- 逐 k 掃描：對 `k=1..k_max` 全掃描產出候選（目前為「≤k 的所有候選」，可加入分組或逐 k 報表）。
- 更多起點策略：雙人/三人起點、隨機起點多次嘗試提升解覆蓋率。
- 其他 backup_strategy：
  - `step_relax`：0→15→30 分階放寬，合併候選。
  - `widen_slot_min`：臨時調整 SLOT_MIN（與 DB/需求一致性需注意）。
  - `shift_window`：對每段時間左右滑動 ±n 分鐘嘗試匹配。

3) 約束支援
- `must_include`/`must_exclude`、標籤/屬性匹配（case-employee metadata）。
- 人員上限：每日/每週時數上限，連續上班最長限制等。

4) 效能與可擴充性
- bitset 取代 set 以加速大型時槽運算。
- 更細的快取：`emp_slots`、`need_slots` 跨 case/放寬重用；Profile 熱點並行化。
- DB 索引與查詢優化；大週期資料分段載入。

5) 輸出與格式
- 同步輸出 `shift_candidates_{ts}.json`：包含排序後的候選、來源 case、標記（baseline/relax）與覆蓋明細，利於後處理。
- 報表分組：依 k 分組、或只輸出前 N 個候選；摘要版與詳細版。

6) 日誌與互動體驗
- 進度條/百分比（例如處理種子 i/N）。
- 非互動模式下的自動放寬策略（以 `backup_strategy` 或環境變數控制）。

7) 測試
- 單元測試：`_inflate_intervals`、`generate_candidates`、tie-break 正確性、排序穩定性。
- 邊界條件：跨日/跨週、同時段重疊、空 case、極大案例。

8) 文件與範例
- 提供 `examples/shift_req.json` 多種場景。
- 在 README/TODO 中記錄策略與排序規則，讓非互動產出可預期。

## 實作指引（如何加）

檔案切入點：
- `src/try_shift.py`
  - 擴充 `CaseReq`（schema 新欄位加入這裡）。
  - 新增/調整 `generate_candidates(...)` 的種子產生與排序規則。
  - 新增 backup 策略：在 `handle_case`（或 `handle_case2`）中讀取 `case.backup_strategy`，呼叫對應產生器並合併去重。
  - 若要逐 k 掃描：新增 `generate_candidates_by_k(req, k_max)`，外層彙整排序輸出。
- `src/orchestrator.py`
  - Stage 5 入口可改成讀取使用者輸入之 `db_path`、`req_path`、是否啟用 `backup_strategy` 等。

關鍵函式（現有）：
- `_inflate_intervals(days, specific, week_any)`：將需求展成 datetime 區間。
- `generate_candidates(req_intervals, k, max_seeds, tag)`：多起點貪婪產生候選、去重與排序。
- `_merge_slots(slots)` / `_format_intervals(...)`：將離散時槽合併為可讀區間並格式化。

擴充排序規則：
- 調整 `Candidate` 結構（例如加入「歷史合作次數」、「地點相近度」）→ 更新 sort key `(size, workload_min, ...)`。

新增 backup 策略範例：
```jsonc
{
  "case_id": "Case-X",
  "k": 3,
  "days": { "mon": ["09:00-12:00"] },
  "backup_strategy": { "type": "step_relax", "minutes": 15, "steps": [0, 15, 30] }
}
```
實作：在 `handle_case` 解析 `type`，針對 `step_relax` 迭代 `steps`，呼叫 `generate_candidates`，合併候選去重與排序。

## 驗收標準
- 文字與 JSON 報表皆正確且排序一致；所有候選完整列出。
- 無解情境時正確進入 backup/互動放寬並產生候選或提示。
- 單元測試涵蓋主要路徑，CI/本地 `py -m py_compile` 無語法錯。

## 風險與注意
- 時區/DST：若未來班表跨時區或夏令時間，需以 tz-aware datetime 處理。
- DB Schema 差異：目前 `try_shift` 使用 `week_shift_import` 的 `shifts` 表；若併入其他來源需加轉換層。
- 編碼問題：對 UI 文字（例如「登入」「確認」）盡量改以穩定 selector（data-testid/role），並集中常數管理。

---
有任何優先順序或策略調整，直接修改本 TODO 並告知，我會依此實作與提交。

