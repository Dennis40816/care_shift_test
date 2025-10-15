# login.py
# Flow: open login -> fill account/password -> OCR 4-digit captcha -> click login.
# Returns the live Page so caller can continue with the same Chromium instance.

from __future__ import annotations
from pathlib import Path
from datetime import datetime
import re
from typing import Optional

from playwright.sync_api import Page, Locator
from care_shift_test.utils import log
from care_shift_test.ocr import read_4digit_from_png_bytes
from stack import start_stack, stop_stack

# --- Hard-coded site + credential for current phase ---
URL_LOGIN = "https://luna.compal-health.com/login"
ACCOUNT = "jdxb041"
PASSWORD = "Emma80625"

# --- Site selectors (tight and explicit) ---
ACCOUNT_SEL = "#account"
PASSWORD_SEL = "#password"

CASE_URL_GLOB = "**/case**"
APP_SHELL_SEL = ".MainFrame .SideBar"
SHIFT_LINK_SEL = "a.NavButton[href='/shift']"

# Real captcha is the 120x50 SVG next to #verificationCode
CAPTCHA_SVG_SEL = "div:has(#verificationCode) + div > svg[width='120'][height='50'][viewBox^='0,0,120,50']"
CAPTCHA_INPUT_SEL = "#verificationCode"

def _solve_and_fill_captcha(page: Page, dump_dir: str | None = None) -> None:
    """Wait for the real 120x50 SVG, OCR with retries, auto-refresh on failure."""
    sel = CAPTCHA_SVG_SEL
    img = page.locator(sel).first
    try:
        img.wait_for(state="visible", timeout=5000)  # ensure rendered
    except Exception:
        log("INFO", "captcha", "captcha svg not visible"); return

    # retry up to 3 times with '重新取得'
    for attempt in range(1, 4):
        png = img.screenshot(type="png")

        if dump_dir:
            Path(dump_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            Path(dump_dir, f"captcha_{ts}_try{attempt}.png").write_bytes(png)

        code = read_4digit_from_png_bytes(png)
        if len(code) == 4:
            page.locator(CAPTCHA_INPUT_SEL).fill(code)
            log("INFO", "captcha", f"OCR ok on try {attempt}")
            return

        # refresh and wait for svg to change
        log("WARN", "captcha", f"OCR failed on try {attempt}, refreshing")
        refresh = page.get_by_role("button", name="重新取得").first
        if refresh.count() == 0:
            # fallback: just small delay then retry
            page.wait_for_timeout(600)
        else:
            old_html = img.evaluate("el => el.outerHTML")
            refresh.click()
            # wait until outerHTML changes (new captcha)
            page.wait_for_function(
                "(selector, oldHtml) => { const el = document.querySelector(selector); return el && el.outerHTML !== oldHtml; }",
                (sel, old_html),
                timeout=5000
            )
            img = page.locator(sel).first  # rebind locator

    raise RuntimeError("captcha OCR failed after 3 attempts")

def _wait_until_case_loaded(page: Page) -> None:
    """Wait until /case routed and the app shell is attached."""
    # 先等路由，失敗也不中斷，後續用殼層選擇器保險
    try:
        page.wait_for_url(CASE_URL_GLOB, timeout=15000)
    except Exception:
        pass
    # 等主框架與側欄出現在 DOM（不要求可見，避免被 modal 蓋住）
    page.wait_for_selector(APP_SHELL_SEL, state="attached", timeout=8000)
    page.wait_for_selector(SHIFT_LINK_SEL, state="attached", timeout=8000)

def _click_login(page: Page) -> None:
    """Click the real login button."""
    btn = page.get_by_test_id("loginButton").first
    if btn.count() == 0:
        btn = page.locator("button[title='登入']").first
    if btn.count() == 0:
        btn = page.get_by_role("button", name="登入").first
    if btn.count() == 0:
        raise RuntimeError("login button not found")
    btn.scroll_into_view_if_needed()
    btn.click()

def login(page: Page, dump_captcha_dir: str | None = None) -> Page:
    """Perform login on the given Page. Return the same Page."""
    log("INFO", "login", f"open {URL_LOGIN}")
    page.goto(URL_LOGIN, wait_until="domcontentloaded")
    page.fill(ACCOUNT_SEL, ACCOUNT)
    page.fill(PASSWORD_SEL, PASSWORD)
    _solve_and_fill_captcha(page, dump_dir=dump_captcha_dir)
    _click_login(page)
    try:
        _wait_until_case_loaded(page)
    except Exception:
        _wait_until_case_loaded(page)
    log("INFO", "login", f"url={page.url!r} title={page.title()!r}")
    return page

if __name__ == "__main__":
    # Standalone test: starts one Chromium, logs in, then waits for manual close.
    pw, br, ctx, page = start_stack()
    try:
        login(page)  # returns same page
        log("INFO", "login", "logged in; press Enter to exit")
        input()
    finally:
        stop_stack(pw, br, ctx)
