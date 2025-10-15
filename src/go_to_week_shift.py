# go_to_week_shift.py
from __future__ import annotations
import time
from playwright.sync_api import Page
from care_shift_test.utils import log

# --- Stable selectors ---
NAV_SEL = "a.NavButton[href='/shift']"
MODAL_SEL = (
    ".latest-release-rote-modal.in.modal, "
    "[role='dialog'].modal.in, "
    "[role='dialog'][class*='modal'][style*='display: block']"
)
BACKDROP_SEL = ".modal-backdrop.in, .latest-release-rote-modal__backdrop.in"

# --- Tunables ---
QUIET_MS_DEFAULT = 3000          # quiet window between modal waves
MAX_MODAL_WAVES = 12             # safety cap when draining waves
NAV_TOTAL_TIMEOUT_S = 10.0       # overall navigation time budget
CLICK_TIMEOUT_MS = 400           # short click timeout per attempt
URL_WAIT_TIMEOUT_MS = 1200       # short wait for route change

def _has_blocking_modal(page: Page) -> bool:
    """Return True if any modal/backdrop is currently blocking interaction."""
    try:
        if page.evaluate("document.body && document.body.classList.contains('modal-open')"):
            return True
    except Exception:
        pass
    loc = page.locator(f"{BACKDROP_SEL}, {MODAL_SEL}")
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False

def _close_one_modal_wave(page: Page, *, dont_show_again: bool) -> None:
    """Close the visible modal wave. Toggle 'do not show again' if checkbox exists."""
    dlg = page.locator(MODAL_SEL).first
    if dlg.count() == 0:
        dlg = page.locator("[role='dialog'][class*='modal']").first

    if dlg.count() > 0:
        footer = dlg.locator(".modal-footer")

        # Try to set "do not show again" checkbox
        cb = footer.locator("input[type='checkbox'][value='on']").first
        if cb.count():
            try:
                cb.set_checked(bool(dont_show_again))
            except Exception:
                cb.click()

        # Confirm button or ESC fallback
        btn = footer.locator("button.btn.btn-primary, button:has-text('確定')").first
        if btn.count():
            btn.click()
            log("INFO", "modal", "confirm clicked")
        else:
            page.keyboard.press("Escape")
    else:
        # Backdrop only: try ESC then a tiny click
        page.keyboard.press("Escape")
        try:
            page.locator(BACKDROP_SEL).first.click(position={"x": 1, "y": 1})
        except Exception:
            pass

    # Wait this wave to disappear
    try:
        page.wait_for_function("!document.body.classList.contains('modal-open')", timeout=3000)
    except Exception:
        pass
    try:
        page.wait_for_selector(f"{BACKDROP_SEL}, {MODAL_SEL}", state="hidden", timeout=3000)
    except Exception:
        pass

def _quiet_window_with_countdown(page: Page, quiet_ms: int = QUIET_MS_DEFAULT, poll_ms: int = 200) -> bool:
    """
    Wait for a full quiet window with no modals.
    Returns True if completed the window; False if interrupted by a modal.
    Emits a simple second-level countdown via log().
    """
    end = time.monotonic() + quiet_ms / 1000.0
    last_secs = None
    while True:
        if _has_blocking_modal(page):
            log("INFO", "modal", "interruption detected; quiet aborted")
            return False

        remain = end - time.monotonic()
        secs = int(remain) + 1 if remain > 0 else 0
        if secs != last_secs:
            log("INFO", "modal", f"quiet countdown: {secs}s")
            last_secs = secs

        if remain <= 0:
            return True

        page.wait_for_timeout(min(poll_ms, int(remain * 1000)))

def close_notifications(page: Page, quiet_ms: int = QUIET_MS_DEFAULT, *, dont_show_again: bool = True) -> None:
    """
    Drain all sequential modals:
      - If any is up, close it.
      - Otherwise wait a quiet window; if interrupted, close and repeat.
      - Stop after the quiet window completes or after MAX_MODAL_WAVES rounds.
    """
    try:
        rounds = 0
        while rounds < MAX_MODAL_WAVES:
            if _has_blocking_modal(page):
                _close_one_modal_wave(page, dont_show_again=dont_show_again)
                rounds += 1
                continue

            if _quiet_window_with_countdown(page, quiet_ms=quiet_ms):
                break  # fully quiet within the window

            _close_one_modal_wave(page, dont_show_again=dont_show_again)
            rounds += 1
    except Exception as e:
        log("WARN", "modal", f"close failed: {e}")

def _find_shift_link(page: Page):
    """Get locator for the '總班表' link, trying several strategies."""
    link = page.locator(NAV_SEL).first
    if link.count() > 0:
        return link

    alt = page.get_by_role("link", name="總班表").first
    if alt.count() > 0:
        return alt

    alt2 = page.locator("a[href='/shift']").first
    if alt2.count() > 0:
        return alt2

    return None

def go_to_week_shift(page: Page, *, dont_show_again: bool = True) -> Page:
    """
    Navigate to /shift using the current session.
    Precondition: login already completed on this Page.
    Behavior: drain modals with a 2s quiet window before trying to click.
    """
    # Pre-drain modals so the first click is not intercepted
    close_notifications(page, quiet_ms=QUIET_MS_DEFAULT, dont_show_again=dont_show_again)

    if "/shift" in page.url:
        return page

    link = _find_shift_link(page)
    if not link:
        raise RuntimeError("cannot find the '總班表' link")

    # Guarded navigation loop: drain -> click -> wait route or retry
    deadline = time.monotonic() + NAV_TOTAL_TIMEOUT_S
    while time.monotonic() < deadline:
        if _has_blocking_modal(page):
            close_notifications(page, quiet_ms=QUIET_MS_DEFAULT, dont_show_again=dont_show_again)
            continue

        try:
            link.scroll_into_view_if_needed()
            link.click(timeout=CLICK_TIMEOUT_MS)
        except Exception:
            # Likely intercepted by a sudden modal; loop will drain then retry
            continue

        try:
            page.wait_for_url("**/shift*", timeout=URL_WAIT_TIMEOUT_MS)
            log("INFO", "go_to_week_shift", f"arrived {page.url}")
            return page
        except Exception:
            # No route change yet; loop will re-check modals and retry
            continue

    raise TimeoutError("navigate to /shift timed out")
