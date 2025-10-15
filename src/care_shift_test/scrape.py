from __future__ import annotations
from playwright.sync_api import sync_playwright, Page
from datetime import datetime
from typing import List
from . import config
from .utils import log
from .ocr import read_4digit_from_png_bytes
from .db import Shift

def _navigate_to_date(page: Page, dt: datetime) -> None:
    # Default: direct URL template. Replace with site-specific clicks if needed.
    url = config.SCHEDULE_URL_TEMPLATE.format(yyyy=dt.year, mm=f"{dt.month:02d}", dd=f"{dt.day:02d}")
    page.goto(url, wait_until="domcontentloaded")

def login_and_scrape(base_url: str, username: str, password: str, target_date: datetime) -> List[Shift]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # 1) Login
        log("INFO", "login", "open login page")
        page.goto(base_url, wait_until="domcontentloaded")
        page.fill(config.SELECTOR_USERNAME, username)
        page.fill(config.SELECTOR_PASSWORD, password)

        # 2) OCR if present
        try:
            img = page.locator(config.SELECTOR_CAPTCHA_IMG)
            if img.count() > 0:
                png = img.screenshot(type="png")
                code = read_4digit_from_png_bytes(png)
                if len(code) != 4:
                    # Fallback to manual prompt in terminal
                    log("WARN", "captcha", "OCR failed, ask user")
                    code = input("Enter 4-digit captcha: ").strip()
                page.fill(config.SELECTOR_CAPTCHA_INPUT, code)
        except Exception:
            pass

        page.click(config.SELECTOR_LOGIN_BUTTON)
        page.wait_for_load_state("domcontentloaded")

        # 3) Jump to date
        _navigate_to_date(page, target_date)

        # 4) Parse shifts
        rows = page.locator(config.ROW_SELECTOR)
        n = rows.count()
        out: List[Shift] = []
        for i in range(n):
            row = rows.nth(i)
            try:
                emp = row.locator(config.COL_EMP_NAME).inner_text().strip()
                st = row.locator(config.COL_START).inner_text().strip()
                et = row.locator(config.COL_END).inner_text().strip()
                st_dt = datetime.fromisoformat(st) if "T" in st else datetime.fromisoformat(st.replace(" ", "T"))
                et_dt = datetime.fromisoformat(et) if "T" in et else datetime.fromisoformat(et.replace(" ", "T"))
                out.append(Shift(emp_name=emp, start=st_dt, end=et_dt))
            except Exception:
                # Skip malformed row
                continue

        ctx.close()
        browser.close()
        log("INFO", "scrape", f"parsed {len(out)} shifts")
        return out
