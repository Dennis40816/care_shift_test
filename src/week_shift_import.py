# week_shift_import.py
from __future__ import annotations
import sqlite3, json, re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from playwright.sync_api import Page

from care_shift_test.utils import log
import time

# ---------- DOM → raw rows ----------

_JS_EXTRACT = r"""
() => {
  const rows = Array.from(document.querySelectorAll('tbody > tr'));
  const timeRe = /(\d{2}:\d{2})-(\d{2}:\d{2})/;

  const out = [];
  for (const tr of rows) {
    const uc = tr.querySelector('.user-col');
    if (!uc) continue;
    const a = uc.querySelector('a[href^="/employee/"]');
    const emp_id = a ? (a.getAttribute('href') || '').split('/').pop() : null;
    const emp_name = (a ? a.textContent : uc.textContent || '').trim();

    const tds = Array.from(tr.querySelectorAll('td')).slice(1, 8); // 7 days
    tds.forEach((td, idx) => {
      const cards = Array.from(td.querySelectorAll('.CalendarCard'));
      for (const card of cards) {
        const header = card.querySelector('.CalendarCard__header');
        const body = card.querySelector('.CalendarCard__body');
        const client = header?.querySelector('.CalendarCard__name')?.textContent.trim() || '';

        const otEl = header?.querySelector('.CalendarCard__overtime');
        const overtime = otEl ? (otEl.getAttribute('title') || otEl.textContent.trim() || null) : null;

        const timeText = card.querySelector('.CalendarCard__time')?.textContent.replace(/\s+/g,' ').trim() || '';
        const m = timeText.match(timeRe);
        const start = m ? m[1] : null, end = m ? m[2] : null;

        const statuses = Array.from(card.querySelectorAll('.CalendarCard__status, .CalendarCard__status--success, .CalendarCard__status--danger'))
                              .map(el => ({ cls: el.className, text: el.textContent.trim() }))
                              .filter(s => s.text);

        const bodyFlags = Array.from(body?.classList || []).filter(c => c.startsWith('CalendarCard__body--'));
        const icons = Array.from(card.querySelectorAll('.CalendarCard__icons [title]')).map(el => el.getAttribute('title') || '');
        const labels = Array.from(card.querySelectorAll('[class*="CalendarCard__label"]')).map(el => el.textContent.trim());

        out.push({
          emp_id, emp_name,
          day_index: idx,
          client, start, end, timeText,
          overtime,
          statuses, bodyFlags, icons, labels
        });
      }
    });
  }
  return out;
}
"""

def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)

def _infer_status_flag(body_flags: List[str], statuses: List[Dict[str, str]]) -> Optional[str]:
    # one compact flag for查詢：normal / employee-leave / case-not-found / breath-*
    for f in body_flags:
        if f.endswith("--employee-leave"):
            return "employee-leave"
        if f.endswith("--case-not-found"):
            return "case-not-found"
        if f.startswith("CalendarCard__body--breath-"):
            return f.split("--", 1)[1]  # e.g. breath-GA
    # fallback by statuses text
    texts = " ".join(s["text"] for s in statuses)
    if "請假" in texts:
        return "leave"
    return "normal" if texts else None

def extract_week_raw(page: Page) -> List[Dict[str, Any]]:
    """Read DOM and return raw card rows with day_index and time strings."""
    return page.evaluate(_JS_EXTRACT)

# ---------- DB schema ----------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS employees (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_id   TEXT REFERENCES employees(id) ON DELETE SET NULL,
  employee_name TEXT NOT NULL,
  date          TEXT NOT NULL,                -- YYYY-MM-DD
  start_min     INTEGER NOT NULL,             -- minutes since 00:00
  end_min       INTEGER NOT NULL,
  client_name   TEXT,
  schedule_type TEXT,                         -- from statuses text (first neutral)
  status_flag   TEXT,                         -- compact flag for search
  overtime      TEXT,                         -- header overtime title/text
  raw_time      TEXT,                         -- original time text
  meta          TEXT,                         -- JSON: statuses/bodyFlags/icons/labels
  created_at    TEXT DEFAULT (datetime('now')),
  UNIQUE(employee_id, date, start_min, end_min, client_name)
);
CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(date);
CREATE INDEX IF NOT EXISTS idx_shifts_emp_date ON shifts(employee_name, date);
CREATE INDEX IF NOT EXISTS idx_shifts_status ON shifts(status_flag);
"""

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA_SQL)
    return conn

# ---------- Normalize + import ----------

def _coerce_week_start(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()

def _first_neutral_status(statuses: List[Dict[str, str]]) -> Optional[str]:
    for s in statuses:
        if "CalendarCard__status--" not in s["cls"]:
            return s["text"] or None
    return None

def normalize_rows(raw: List[Dict[str, Any]], week_start: date) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in raw:
        if not r.get("start") or not r.get("end"):
            continue  # skip malformed
        d = week_start + timedelta(days=int(r["day_index"]))
        start_min = _hhmm_to_min(r["start"])
        end_min = _hhmm_to_min(r["end"])
        status_flag = _infer_status_flag(r.get("bodyFlags") or [], r.get("statuses") or [])
        schedule_type = _first_neutral_status(r.get("statuses") or [])
        out.append({
            "employee_id": r.get("emp_id"),
            "employee_name": r.get("emp_name") or "",
            "date": d.strftime("%Y-%m-%d"),
            "start_min": start_min,
            "end_min": end_min,
            "client_name": r.get("client") or None,
            "schedule_type": schedule_type,
            "status_flag": status_flag,
            "overtime": r.get("overtime"),
            "raw_time": r.get("timeText"),
            "meta": json.dumps({
                "statuses": r.get("statuses") or [],
                "bodyFlags": r.get("bodyFlags") or [],
                "icons": r.get("icons") or [],
                "labels": r.get("labels") or []
            }, ensure_ascii=False),
        })
    return out

def upsert_employees(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    to_upsert = {(r["employee_id"], r["employee_name"]) for r in rows if r.get("employee_id")}
    if not to_upsert:
        return
    conn.executemany(
        "INSERT INTO employees(id, name) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=datetime('now')",
        list(to_upsert),
    )

def upsert_shifts(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    cur = conn.executemany(
        """
        INSERT INTO shifts
          (employee_id, employee_name, date, start_min, end_min, client_name,
           schedule_type, status_flag, overtime, raw_time, meta)
        VALUES
          (:employee_id, :employee_name, :date, :start_min, :end_min, :client_name,
           :schedule_type, :status_flag, :overtime, :raw_time, :meta)
        ON CONFLICT(employee_id, date, start_min, end_min, client_name)
        DO UPDATE SET
          employee_name = excluded.employee_name,
          schedule_type = excluded.schedule_type,
          status_flag   = excluded.status_flag,
          overtime      = excluded.overtime,
          raw_time      = excluded.raw_time,
          meta          = excluded.meta
        """,
        rows,
    )
    return cur.rowcount or 0

def set_people_page_size(page: Page, size: int = 100,
                         timeout_ms: int = 12000,
                         stable_ms: int = 5000,
                         poll_ms: int = 200) -> int:
    btn = page.locator("#pageDropDown").first
    btn.wait_for(state="visible", timeout=timeout_ms)
    btn.scroll_into_view_if_needed()

    rows = page.locator("tbody .user-col")
    try:
        prev = rows.count()
    except Exception:
        prev = -1

    # 展開選單並點選 size
    btn.click()
    menu = page.locator(
        ".dropdown-menu[aria-labelledby='pageDropDown'], "
        "#pageDropDown ~ .dropdown-menu, "
        ".open .dropdown-menu"
    ).first
    menu.wait_for(state="visible", timeout=2000)

    item = menu.locator(f"[role='menuitem'][data-page='{size}']").first
    if item.count() == 0:
        item = page.locator(f"[role='menuitem'][data-page='{size}']").first
    if item.count() == 0:
        raise RuntimeError(f"page-size item not found: data-page='{size}'")

    item.scroll_into_view_if_needed()
    item.click()

    # Phase 1：選單關閉
    page.wait_for_function(
        """
        () => {
          const menu =
            document.querySelector(".dropdown-menu[aria-labelledby='pageDropDown']") ||
            document.querySelector("#pageDropDown ~ .dropdown-menu") ||
            document.querySelector(".open .dropdown-menu");
          return !(menu && menu.offsetParent);
        }
        """,
        timeout=min(3000, timeout_ms)
    )

    # Phase 2：列數連續穩定 stable_ms，含倒計時日誌
    start = time.monotonic()
    last_change = time.monotonic()
    last_rows = -1
    last_logged_sec = None

    def count_rows() -> int:
        try:
            return page.evaluate("() => document.querySelectorAll('tbody .user-col').length")
        except Exception:
            return 0

    last_rows = count_rows()
    last_change = time.monotonic()

    while True:
        n = count_rows()
        now = time.monotonic()
        if n != last_rows:
            last_rows = n
            last_change = now

        stable_secs = now - last_change
        remain = max(0.0, stable_ms/1000.0 - stable_secs)
        # 每秒記一次 log
        sec_int = int(remain) + (1 if remain > 0 else 0)
        if sec_int != last_logged_sec:
            log("INFO", "page-size", f"stable countdown: {sec_int}s, rows={n}")
            last_logged_sec = sec_int

        if stable_secs >= stable_ms/1000.0 and n > 0:
            break

        if (now - start) * 1000 > timeout_ms:
            raise TimeoutError(f"page size change not stabilized in {timeout_ms}ms, rows={n}")

        page.wait_for_timeout(min(poll_ms, int(remain * 1000)) if remain > 0 else poll_ms)

    curr = last_rows
    log("INFO", "page-size", f"set to {size}, rows={curr}, prev={prev}")
    return curr


def import_week_shifts(page: Page, db_path: str, week_start: str | date) -> int:
    """High-level: DOM→rows→DB. week_start is this view's Sunday date."""
    # show 100 employee first
    set_people_page_size(page, 100)
    
    ws = _coerce_week_start(week_start)
    raw = extract_week_raw(page)
    rows = normalize_rows(raw, ws)
    conn = init_db(db_path)
    try:
        upsert_employees(conn, rows)
        n = upsert_shifts(conn, rows)
        conn.commit()
        return n
    finally:
        conn.close()

# ---------- Simple search helpers (optional) ----------

def find_employee_day(conn: sqlite3.Connection, employee_name_like: str, day: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT employee_name, date, start_min, end_min, client_name, schedule_type, status_flag, overtime
        FROM shifts
        WHERE employee_name LIKE ? AND date = ?
        ORDER BY start_min
        """,
        (employee_name_like, day),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

if __name__ == "__main__":
    # Example usage sketch (requires an existing Playwright Page on the week view):
    # from stack import start_stack, stop_stack
    # pw, br, ctx, page = start_stack()
    # ... navigate/login to week view ...
    # count = import_week_shifts(page, "shifts.db", "2025-01-12")  # week Sunday
    # print("upserted rows:", count)
    pass
