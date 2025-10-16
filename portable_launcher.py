from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Determine root path (both for bundled exe and source run)
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent

SRC_DIR = ROOT / "src"
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from orchestrator import main as orchestrator_main  # noqa: E402

APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "CareShift"
BROWSER_DIR = ROOT / "ms-playwright"
SETTINGS_REQ = ROOT / "shift_req.json"
USER_REQ = APP_DIR / "shift_req.json"


def log(msg: str) -> None:
    print(f"[portable] {msg}")


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_REQ.exists() and not USER_REQ.exists():
        shutil.copy2(SETTINGS_REQ, USER_REQ)
        log(f"created default shift_req.json at {USER_REQ}")


def ensure_playwright() -> None:
    if BROWSER_DIR.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_DIR)
        log(f"using packaged Playwright browsers at {BROWSER_DIR}")
        return

    cache_dir = APP_DIR / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache_dir)
    if cache_dir.exists():
        log(f"using cached Playwright browsers at {cache_dir}")
        return

    log("downloading Playwright Chromium (first run)...")
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    if result.returncode != 0:
        raise RuntimeError("Playwright browser download failed")


def ensure_tesseract() -> None:
    if shutil.which("tesseract"):
        return

    log("Tesseract OCR not found. Attempting winget installation...")
    if not shutil.which("winget"):
        log("winget not available; please install Tesseract OCR manually")
        return

    args = [
        "winget",
        "install",
        "-e",
        "--id",
        "UB-Mannheim.TesseractOCR",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    subprocess.run(args)
    if shutil.which("tesseract"):
        log("Tesseract installation completed")
    else:
        log("Tesseract still missing; please install manually from https://github.com/UB-Mannheim/tesseract/wiki")


def main() -> None:
    ensure_app_dir()
    ensure_tesseract()
    ensure_playwright()
    os.chdir(APP_DIR)
    orchestrator_main()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[portable] ERROR: {exc}")
        input("Press Enter to exit...")
        raise
