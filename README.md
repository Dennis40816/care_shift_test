## Care Shift Test

Automates weekly shift handling end‑to‑end: login → navigate → import week → generate shift candidates.

—

### Who you are and how to start

- Dist user (packaged folder):
  - Unzip `dist/care_shift/` and run `run_care_shift.cmd`. No Python needed.
  - Optional (for captcha OCR on login): install Tesseract OCR 5.x (UB‑Mannheim build recommended) and make sure `tesseract.exe` is on PATH.

- Developer (from source, recommended with uv):
  - Install uv (Windows): `winget install -e --id Astral.UV`
  - Setup project and browsers:
    - `uv sync`
    - `uv run python -m playwright install chromium`
      - Linux extra: `uv run python -m playwright install-deps`
  - Run: `uv run python src/orchestrator.py`

Fallback (Windows only): `./install_dependencies.ps1 -InstallTesseract`

—

### Working directory and outputs

- Preferred work dir = the executable folder; fallback = `%APPDATA%\CareShift` if exe folder isn’t writable.
- Results are written to `out/` under the chosen work dir.
- If `shift_req.json` is missing in the work dir, a default copy is created from the exe folder.
- Override output folder with env var `SHIFT_OUT_DIR`.

—

### Running via orchestrator

`run_try_shift.cmd` starts an interactive menu:
- Login → Go to week shift → Import week to DB → Try shift

Make sure `shift_req.json` suits your week/cases before “Try shift”.

—

### Config: shift_req.json (v1)

Minimal example (see `shift_req.example.json` for all options):

```jsonc
{
  "week_date": null,
  "cases": [
    {
      "case_id": "Example",
      "k": 3,
      "slot_min": 30,
      "days": { "mon": ["09:00-12:00"], "wed": ["14:00-16:00"] },
      "specific": [ { "date": "2025-10-17", "ranges": ["16:00-18:00"] } ],
      "backup_strategy": { "type": "relax_minutes", "minutes": 30 },
      "enumerate_all": false,
      "max_team_size": null,
      "include_supersets": false
    }
  ]
}
```

Day keys: `sun, mon, tue, wed, thu, fri, sat`.

—

### Output format

- File: `out/shift_candidates_{timestamp}.txt`
- Candidate ordering: team size → weekly load minutes → team name
- Coverage lines are time‑sorted (not grouped by person):

```
YYYY-MM-DD (一) HH:MM - HH:MM: 員工姓名
```

—

### Build a standalone (Windows)

```powershell
./build_exe.ps1 -Clean
```

Outputs in `dist/care_shift/`:
- `care_shift.exe`, `ms-playwright/`, `run_care_shift.cmd`

Zip and share the whole folder with users.

—

### Dependencies (summary)

- Dist users: Windows 10/11 x64; optional Tesseract OCR 5.x if you use the login stage.
- Developers: uv (recommended), Playwright browsers, optional Tesseract OCR 5.x.

—

### Support

Check console output and files in `out/`. Re‑run `uv sync` or `./install_dependencies.ps1` if environment drifts.
