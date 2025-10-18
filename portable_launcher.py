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

# Preferred working directory: the executable folder (ROOT).
# Fallback to user's APPDATA/CareShift when ROOT is not writable.
BROWSER_DIR = ROOT / "ms-playwright"
APPDATA_BASE = Path(
    os.getenv("APPDATA")
    or os.getenv("LOCALAPPDATA")
    or str(Path.home())
)
APP_DIR = APPDATA_BASE / "CareShift"

SETTINGS_REQ = ROOT / "shift_req.json"
# USER_REQ is resolved against the chosen WORK_DIR later
USER_REQ: Path


def log(msg: str) -> None:
    print(f"[portable] {msg}")


def _is_writable_dir(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        test = p / ".write_test.tmp"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def ensure_work_dir() -> Path:
    """Choose a working directory and ensure default config exists.

    Priority: ROOT (exe folder) if writable, otherwise APP_DIR.
    Always copy default shift_req.json into WORK if it doesn't exist.
    """
    work = ROOT if _is_writable_dir(ROOT) else APP_DIR
    work.mkdir(parents=True, exist_ok=True)
    global USER_REQ
    USER_REQ = work / "shift_req.json"
    if SETTINGS_REQ.exists() and not USER_REQ.exists():
        shutil.copy2(SETTINGS_REQ, USER_REQ)
        log(f"created default shift_req.json at {USER_REQ}")
    if work == APP_DIR and not ROOT.samefile(work):
        log(f"using APPDATA work dir: {work}")
    else:
        log(f"using ROOT work dir: {work}")
    return work


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
    work = ensure_work_dir()
    ensure_tesseract()
    ensure_playwright()
    # Prefer writing outputs in an 'out' subfolder under the chosen work dir
    out_dir = work / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Let the engine know where to put txt outputs (still reads shift_req.json from CWD)
    os.environ["SHIFT_OUT_DIR"] = str(out_dir)
    # Run with CWD at work dir so JSON/DB live next to the exe by default
    os.chdir(work)
    orchestrator_main()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[portable] ERROR: {exc}")
        input("Press Enter to exit...")
        raise
