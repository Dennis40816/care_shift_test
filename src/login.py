# login.py
# Flow: open login -> fill account/password -> OCR 4-digit captcha -> click login.
# Returns the live Page so caller can continue with the same Chromium instance.

from __future__ import annotations
from pathlib import Path
from datetime import datetime
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

# --- Tunables / Magic numbers ---
LOGIN_MAX_ATTEMPTS = 5
CAPTCHA_VISIBLE_TIMEOUT_MS = 8000
CAPTCHA_REFRESH_WAIT_MS = 5000
CAPTCHA_RETRY_PAUSE_MS = 600
LOGIN_BUTTON_DISAPPEAR_MS = 2500

def _login_button_locator(page: Page) -> Locator:
    """Return a resilient locator to the real login button."""
    btn = page.get_by_test_id("loginButton").first
    if btn.count() == 0:
        btn = page.locator("button[title='登入']").first
    if btn.count() == 0:
        btn = page.get_by_role("button", name="登入").first
    return btn

def _locate_captcha_refresh(page: Page) -> Locator:
    """Find the captcha refresh control.

    Primary: the SVG icon (role=img) whose accessible name/title is '重新取得'.
    Fallbacks: title text, FontAwesome data-icon, or a similar-named button.
    """
    loc = page.get_by_role("img", name="重新取得").first
    if loc.count() > 0:
        return loc
    loc = page.locator("svg[role='img']:has(title:text-is('重新取得'))").first
    if loc.count() > 0:
        return loc
    loc = page.locator("svg[data-icon='arrows-rotate']").first
    if loc.count() > 0:
        return loc
    loc = page.get_by_role("button", name="重新取得").first
    if loc.count() > 0:
        return loc
    return page.get_by_role("button", name="重新產生").first
def _fill_captcha_once(page: Page, attempt: int, dump_dir: str | None = None) -> bool:
    """Try a single OCR read and fill. Return True if 4-digit code was filled."""
    sel = CAPTCHA_SVG_SEL
    img = page.locator(sel).first
    try:
        img.wait_for(state="visible", timeout=CAPTCHA_VISIBLE_TIMEOUT_MS)
    except Exception:
        log("INFO", "captcha", "captcha svg not visible")
        return False

    png = img.screenshot(type="png")
    if dump_dir:
        try:
            Path(dump_dir).mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            Path(dump_dir, f"captcha_{ts}_try{attempt}.png").write_bytes(png)
        except Exception:
            pass

    code = read_4digit_from_png_bytes(png)
    if len(code) == 4:
        page.locator(CAPTCHA_INPUT_SEL).fill(code)
        log("INFO", "captcha", f"OCR ok on try {attempt}")
        return True
    log("WARN", "captcha", f"OCR did not yield 4 digits on try {attempt}")
    return False

def _did_login_succeed(page: Page, *, timeout_ms: int = LOGIN_BUTTON_DISAPPEAR_MS) -> bool:
    """Return True if we appear to have left the login page.

    Primary check: the login button disappears. Secondary signals: app shell attached or URL changed.
    """
    # Prefer the explicit signal: login button detached/hidden
    try:
        # Wait a bit for the button to disappear after click
        btn = _login_button_locator(page)
        if btn.count() > 0:
            try:
                btn.wait_for(state="detached", timeout=timeout_ms)
                return True
            except Exception:
                # If still attached, check visibility
                try:
                    if not btn.is_visible():
                        return True
                except Exception:
                    pass
    except Exception:
        # Locator resolution failed; treat as likely navigated
        return True

    # Fallbacks: shell attached or url matches
    try:
        page.wait_for_selector(APP_SHELL_SEL, state="attached", timeout=800)
        return True
    except Exception:
        pass
    try:
        page.wait_for_url(CASE_URL_GLOB, timeout=800)
        return True
    except Exception:
        pass
    return False

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

def login(page: Page, dump_captcha_dir: str | None = None, *, max_attempts: int = LOGIN_MAX_ATTEMPTS) -> Page:
    """Login using OCR and refresh via the '重新取得' SVG icon; verify by login button disappearance.

    Retries up to max_attempts.
    """
    log("INFO", "login", f"open {URL_LOGIN}")
    page.goto(URL_LOGIN, wait_until="domcontentloaded")
    page.fill(ACCOUNT_SEL, ACCOUNT)
    page.fill(PASSWORD_SEL, PASSWORD)

    # Prepare captcha elements
    sel = CAPTCHA_SVG_SEL
    img = page.locator(sel).first
    try:
        img.wait_for(state="visible", timeout=CAPTCHA_VISIBLE_TIMEOUT_MS)
    except Exception:
        log("WARN", "captcha", "captcha svg not visible; proceeding anyway")

    last_html: Optional[str] = None
    if img.count() > 0:
        try:
            last_html = img.evaluate("el => el.outerHTML")
        except Exception:
            last_html = None

    for attempt in range(1, max_attempts + 1):
        # On retries, click the '重新取得' refresh icon and wait for change
        if attempt > 1:
            try:
                refresh = _locate_captcha_refresh(page)
                if refresh.count() > 0:
                    prev = last_html or (img.evaluate("el => el.outerHTML") if img.count() > 0 else "")
                    try:
                        refresh.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    try:
                        refresh.click()
                    except Exception:
                        try:
                            refresh.locator("xpath=ancestor-or-self::button[1]").click()
                        except Exception:
                            pass
                    page.wait_for_function(
                        "(selector, oldHtml) => { const el = document.querySelector(selector); return el && el.outerHTML !== oldHtml; }",
                        (sel, prev),
                        timeout=CAPTCHA_REFRESH_WAIT_MS,
                    )
                    img = page.locator(sel).first
                    try:
                        last_html = img.evaluate("el => el.outerHTML")
                    except Exception:
                        last_html = None
                else:
                    page.wait_for_timeout(CAPTCHA_RETRY_PAUSE_MS)
            except Exception:
                page.wait_for_timeout(CAPTCHA_RETRY_PAUSE_MS)

        # Try a single OCR read + fill
        if not _fill_captcha_once(page, attempt, dump_dir=dump_captcha_dir):
            continue

        # Click login and check success by button disappearance
        _click_login(page)
        if _did_login_succeed(page, timeout_ms=LOGIN_BUTTON_DISAPPEAR_MS):
            try:
                _wait_until_case_loaded(page)
            except Exception:
                _wait_until_case_loaded(page)
            log("INFO", "login", f"success on attempt {attempt}, url={page.url!r}")
            log("INFO", "login", f"title={page.title()!r}")
            return page
        else:
            log("WARN", "login", f"attempt {attempt} failed; retrying")

    raise RuntimeError(f"login failed after {max_attempts} attempts")

if __name__ == "__main__":
    # Standalone test: starts one Chromium, logs in, then waits for manual close.
    pw, br, ctx, page = start_stack()
    try:
        login(page)  # returns same page
        log("INFO", "login", "logged in; press Enter to exit")
        input()
    finally:
        stop_stack(pw, br, ctx)



