# stack.py
# Purpose: own a single Chromium instance you can reuse across modules.

from __future__ import annotations
from typing import Tuple
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

HEADLESS = False  # set True for CI

def start_stack(headless: bool = HEADLESS) -> Tuple[object, Browser, BrowserContext, Page]:
    """Start Playwright -> Chromium -> context -> page. Return all handles."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context()
    page = context.new_page()
    return pw, browser, context, page

def stop_stack(pw: object, browser: Browser, context: BrowserContext) -> None:
    """Close in safe order."""
    try:
        context.close()
    finally:
        try:
            browser.close()
        finally:
            pw.stop()
