param(
    [string]$VenvPath = ".venv",
    [string]$OutputName = "care_shift",
    [switch]$Clean
)

function Write-Step($msg) { Write-Host "[build] $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[build] ERROR: $msg" -ForegroundColor Red; exit 1 }

$projectRoot = Get-Location
$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Fail "virtual env not found at $venvPython. Run install_dependencies.ps1 first." }

if ($Clean) {
    Write-Step "cleaning previous build artifacts"
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

Write-Step "ensuring PyInstaller is available"
& $venvPython -m pip install --upgrade pyinstaller | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "failed to install/upgrade PyInstaller" }

$buildDir = Join-Path $projectRoot "build"
$browserCache = Join-Path $buildDir "ms-playwright"
if (Test-Path $browserCache) { Remove-Item -Recurse -Force $browserCache }
New-Item -ItemType Directory -Force -Path $browserCache | Out-Null

Write-Step "installing Playwright browsers into build cache"
$env:PLAYWRIGHT_BROWSERS_PATH = $browserCache
& $venvPython -m playwright install chromium | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "playwright browser install failed" }

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
    "--hidden-import", "try_shift_core",
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
