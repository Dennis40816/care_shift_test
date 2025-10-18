param(
    [string]$VenvPath = ".venv",
    [string]$OutputName = "care_shift",
    [switch]$Clean,
    [Alias('delete-playwright')]
    [switch]$DeletePlaywright
)

function Write-Step($msg) { Write-Host "[build] $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[build] ERROR: $msg" -ForegroundColor Red; exit 1 }

$projectRoot = Get-Location
$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Fail "virtual env not found at $venvPython. Run install_dependencies.ps1 first." }

# Prepare build/dist paths early so we can do selective cleaning
$buildDir = Join-Path $projectRoot "build"
$browserCache = Join-Path $buildDir "ms-playwright"
$distRoot = Join-Path $projectRoot "dist"

if ($Clean) {
    Write-Step "cleaning previous build artifacts"
    # Always clear dist
    if (Test-Path $distRoot) { Remove-Item -Recurse -Force $distRoot -ErrorAction SilentlyContinue }
    # For build: keep ms-playwright unless DeletePlaywright is set
    if (Test-Path $buildDir) {
        if ($DeletePlaywright) {
            Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue
        } else {
            Get-ChildItem -Path $buildDir -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne 'ms-playwright' } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Step "ensuring PyInstaller is available"
& $venvPython -m pip install --upgrade pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "failed to install/upgrade PyInstaller" }

if ($DeletePlaywright) {
    Write-Step "deleting existing Playwright browsers cache"
    Remove-Item -Recurse -Force $browserCache -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $browserCache | Out-Null

$env:PLAYWRIGHT_BROWSERS_PATH = $browserCache

# Reuse existing browsers if present to speed up builds
$chromiumDirs = @(Get-ChildItem -Path $browserCache -Directory -Filter 'chromium*' -ErrorAction SilentlyContinue)
if ($chromiumDirs.Count -gt 0) {
    Write-Step "reusing Playwright browsers in build cache ($($chromiumDirs.Count) found). Use -DeletePlaywright to refresh."
} else {
    Write-Step "installing Playwright browsers into build cache"
    & $venvPython -m playwright install chromium | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "playwright browser install failed" }
}

Write-Step "running PyInstaller"
$pyinstallerArgs = @(
    "portable_launcher.py",
    "--name", $OutputName,
    "--clean",
    "--noconfirm",
    "--onedir",
    "--collect-all", "playwright",
    "--hidden-import", "care_shift_test.ocr",
    "--hidden-import", "care_shift_test.utils",
    "--hidden-import", "try_shift_engine",
    "--add-data", "shift_req.json;.",
    "--add-data", "shift_req.example.json;.",
    "--add-data", "README.md;."
)
& $venvPython -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed" }

$distDir = Join-Path $projectRoot "dist" | Join-Path -ChildPath $OutputName
if (-not (Test-Path $distDir)) { Fail "expected dist folder $distDir not found" }

Write-Step "copying bundled playwright browsers"
Copy-Item -Recurse -Force -Path $browserCache -Destination (Join-Path $distDir "ms-playwright")

Write-Step "ensuring JSON templates exist in dist"
$jsonFiles = @(
    @{ Name = "shift_req.json";         Template = $null },
    @{ Name = "shift_req.example.json";  Template = @'
{
  "notes": "Example shift_req.json showcasing all supported fields. Day keys: sun, mon, tue, wed, thu, fri, sat. Use 'notes' fields for explanations (keeps JSON valid).",
  "week_date": null,
  "cases": [
    {
      "case_id": "Full-Options",
      "notes": "Full demo with all optional fields set. 'thu' has >2 ranges to demonstrate auto-bump to k=4 internally when needed.",
      "week_date": "2025-10-13",
      "k": 3,
      "slot_min": 30,
      "days": {
        "sun": ["08:30-09:30"],
        "mon": ["16:00-18:00"],
        "tue": ["09:30-10:00", "14:00-17:30"],
        "wed": ["15:00-17:00"],
        "thu": ["09:00-10:00", "14:30-16:00", "18:00-20:00"],
        "fri": ["10:00-12:00"],
        "sat": ["13:00-14:30"]
      },
      "specific": [
        { "date": "2025-10-16", "ranges": ["18:00-20:00"] }
      ],
      "backup_strategy": { "type": "relax_minutes", "minutes": 30 },
      "enumerate_all": true,
      "max_team_size": 3,
      "include_supersets": false
    }
  ]
}
'@ }
)
foreach ($f in $jsonFiles) {
    $src = Join-Path $projectRoot $f.Name
    $dst = Join-Path $distDir $f.Name
    if (-not (Test-Path $dst)) {
        if (Test-Path $src) {
            Copy-Item -Force $src $dst
        } elseif ($f.Template) {
            Set-Content -Path $dst -Value $f.Template -Encoding UTF8
        }
    }
}

Write-Step "generating runtime launcher"
$runtimeCmd = @"
@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%SCRIPT_DIR%ms-playwright"
"%SCRIPT_DIR%$OutputName.exe"
if errorlevel 1 pause
endlocal
"@
Set-Content -Path (Join-Path $distDir "run_care_shift.cmd") -Value $runtimeCmd -Encoding ASCII

Write-Step "build completed. Output: $distDir"
Write-Host "Run \"$distDir\run_care_shift.cmd\" on the target machine." -ForegroundColor Green
