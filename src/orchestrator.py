# run_stages.py
# Minimal menu to run specific stages on a single reusable Chromium instance.

from __future__ import annotations
from typing import Callable
from datetime import date, timedelta
from playwright.sync_api import Page
from care_shift_test.utils import log
from stack import start_stack, stop_stack
from login import login2 as login
from go_to_week_shift import go_to_week_shift
from week_shift_import import import_week_shifts

def ensure_page(ctx, page: Page) -> Page:
    """Ensure a live page exists; recreate if closed."""
    if page.is_closed():
        page = ctx.new_page()
    return page

def stage_login(page: Page) -> Page:
    """Run login stage."""
    return login(page, dump_captcha_dir=None)

def stage_shift(page: Page) -> Page:
    """Navigate to weekly shift page."""
    return go_to_week_shift(page)

def _sunday_of(d: date) -> date:
    # Python: Monday=0..Sunday=6 -> previous Sunday
    return d - timedelta(days=(d.weekday() + 1) % 7)

def stage_import(page: Page) -> Page:
    """Import current week view into SQLite."""
    # 提示參數（簡單、可擴充）
    default_db = "shifts.db"
    default_week = _sunday_of(date.today()).strftime("%Y-%m-%d")

    ## TODO: 增加這個功能
    # db_path = (input(f"DB path [{default_db}]: ").strip() or default_db)
    # week_start = (input(f"Week start Sunday YYYY-MM-DD [{default_week}]: ").strip() or default_week)

    db_path = default_db
    week_start = _sunday_of(date.today()).strftime("%Y-%m-%d")
    
    # 直接匯入；假設目前就在週檢視。若不是，請先跑 stage_shift。
    log("INFO", "import", f"start import -> db={db_path}, week_start={week_start}")
    try:
        n = import_week_shifts(page, db_path, week_start)
        log("INFO", "import", f"upserted {n} rows")
    except Exception as e:
        log("ERROR", "import", f"failed: {e}")
        raise
    return page

def stage_all(page: Page) -> Page:
    """Login -> Week shift -> Import DB."""
    page = stage_login(page)
    page = stage_shift(page)
    page = stage_import(page)
    return page

def main() -> None:
    pw, br, ctx, page = start_stack(headless=False)
    try:
        actions: dict[str, tuple[str, Callable[[Page], Page]]] = {
            "1": ("all", stage_all),
            "2": ("login", stage_login),
            "3": ("go_to_week_shift", stage_shift),
            "4": ("import_week_to_db", stage_import),
        }

        while True:
            print("\nSelect stage:")
            print("  [1] all  (login -> go_to_week_shift -> import_week_to_db)")
            print("  [2] login")
            print("  [3] go_to_week_shift")
            print("  [4] import_week_to_db")
            print("  [q] quit")
            choice = input("Choice: ").strip().lower()

            if choice in ("q", "quit"):
                break
            if choice not in actions:
                print("Invalid choice.")
                continue

            name, fn = actions[choice]
            try:
                page = ensure_page(ctx, page)
                log("INFO", "runner", f"start {name}")
                page = fn(page)
                log("INFO", "runner", f"done {name}")
            except Exception as e:
                log("ERROR", "runner", f"{name} failed: {e}")

        input("Press Enter to close browser...")
    finally:
        stop_stack(pw, br, ctx)

if __name__ == "__main__":
    main()
