param(
    [string]$PythonExe = "python",
    [string]$VenvPath = ".venv",
    [switch]$InstallTesseract
)

function Write-Step($message) {
    Write-Host "[install] $message" -ForegroundColor Cyan
}

function Invoke-OrThrow([string]$Command, [string]$ErrorMessage) {
    Write-Step "executing: $Command"
    $process = Start-Process powershell -ArgumentList "-NoLogo", "-NoProfile", "-Command", $Command -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "${ErrorMessage} (exit code $($process.ExitCode))"
    }
}

function Ensure-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-Python([string]$PythonExe) {
    if (Ensure-Command $PythonExe) {
        Write-Step "found python executable '$PythonExe'"
        return
    }

    if (-not (Ensure-Command "winget")) {
        throw "python executable not found. Install Python 3.12+ manually or install winget."
    }

    Write-Step "python not found; installing via winget"
    Invoke-OrThrow "winget install -e --id Python.Python.3.12" "failed to install Python via winget"

    if (-not (Ensure-Command $PythonExe)) {
        throw "python still not available after installation."
    }
}

function Get-VenvPython([string]$VenvPath) {
    return Join-Path $VenvPath "Scripts" | Join-Path -ChildPath "python.exe"
}

try {
    Write-Step "starting dependency installation"
    Ensure-Python $PythonExe

    if (-not (Test-Path $VenvPath)) {
        Write-Step "creating virtual environment at '$VenvPath'"
        Invoke-OrThrow "$PythonExe -m venv `"$VenvPath`"" "failed to create virtual environment"
    } else {
        Write-Step "virtual environment '$VenvPath' already exists"
    }

    $VenvPython = Get-VenvPython $VenvPath
    if (-not (Test-Path $VenvPython)) {
        throw "virtual environment python executable not found at $VenvPython"
    }

    Write-Step "upgrading pip"
    Invoke-OrThrow "`"$VenvPython`" -m pip install --upgrade pip" "failed to upgrade pip"

    Write-Step "installing project dependencies"
    Invoke-OrThrow "`"$VenvPython`" -m pip install -e ." "failed to install project dependencies"

    Write-Step "installing Playwright browser drivers"
    Invoke-OrThrow "`"$VenvPython`" -m playwright install" "failed to install Playwright browsers"

    if ($InstallTesseract -and (Ensure-Command "winget")) {
        Write-Step "installing Tesseract OCR runtime via winget"
        Invoke-OrThrow "winget install -e --id UB-Mannheim.TesseractOCR" "failed to install Tesseract OCR"
    } elseif ($InstallTesseract) {
        Write-Step "winget not found; please install Tesseract OCR manually"
    } else {
        Write-Step "skipping Tesseract OCR installation (use -InstallTesseract to enable)"
    }

    Write-Step "installation complete"
    Write-Host "To activate the environment: `n  powershell -NoLogo -NoProfile -Command \"& `"$VenvPath\Scripts\Activate.ps1`\"\"" -ForegroundColor Green
}
catch {
    Write-Host "[install] ERROR: $_" -ForegroundColor Red
    exit 1
}
