## Care Shift Test – Quick Start

This project automates the Compal weekly shift workflow (login → navigate → import → try_shift report).

### 1. Install Dependencies (Windows 10)

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
./install_dependencies.ps1 -InstallTesseract
```

The script will:

1. Ensure Python 3.12+ is available (installs via `winget` if missing).
2. Create/refresh the `.venv` virtual environment.
3. Install all Python dependencies (`pip install -e .`).
4. Download Playwright browser drivers.
5. Optionally install Tesseract OCR runtime (`-InstallTesseract`).

> **Tip**: rerun the script whenever dependencies need to be refreshed.

### 2. Launch the Orchestrator

```cmd
run_try_shift.cmd
```

This starts the interactive menu:

1. Login
2. Go to week shift
3. Import week to DB
4. Try shift (stage 5)

Make sure `shift_req.json` is configured with your cases before running stage 5.

### 3. shift_req.json Schema (v1)

```jsonc
{
  "week_date": null,                // optional global week anchor
  "cases": [
    {
      "case_id": "Example",
      "week_date": null,           // optional case override
      "k": 3,                      // max employees for the team
      "slot_min": 30,              // minimum slot granularity (minutes)
      "days": {                    // weekly recurring ranges
        "mon": ["09:00-12:00"],
        "wed": ["14:00-16:00"]
      },
      "specific": [
        { "date": "2025-10-17", "ranges": ["16:00-18:00"] }
      ],
      "backup_strategy": {
        "type": "relax_minutes",
        "minutes": 30
      }
    }
  ]
}
```

### 4. Output

Running stage 5 writes `shift_candidates_{timestamp}.txt` with all feasible teams ordered by:

1. Team size (k) – smaller is better.
2. Weekly load minutes – lower means less overall workload.
3. Team members (alphabetical) for deterministic ties.

Each coverage line lists the employee and merged time blocks, e.g. `- 陳怡婷: (一) 2025-10-13 16:00 - 18:00`.

### 5. Helpful Commands

- Reinstall dependencies: `./install_dependencies.ps1`
- Update Playwright: `.".venv\Scripts\python.exe" -m playwright install`
- Run orchestrator without script: `.".venv\Scripts\python.exe" src\orchestrator.py`

### 6. Build Standalone Package (PyInstaller)

1. Ensure dependencies are installed (`install_dependencies.ps1`) and browsers downloaded.
2. Execute:

   ```powershell
   ./build_exe.ps1 -Clean
   ```

3. Result is under `dist/care_shift/`:
   - `care_shift.exe`
   - `ms-playwright/` (bundled Chromium)
   - `run_care_shift.cmd` (sets `PLAYWRIGHT_BROWSERS_PATH` then launches exe)

Copy the whole folder to the target machine and run `run_care_shift.cmd` to launch without installing Python.

### 7. Support

If anything fails, re-run the installer and review logs in `shift_candidates_*.txt` or the console. The `TODO.md` file tracks roadmap items for future enhancements.
