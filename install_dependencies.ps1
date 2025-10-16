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

function Get-PythonVersion([string]$Command) {
    try {
        $parts = $Command -split ' '
        $exe = $parts[0]
        $args = @()
        if ($parts.Length -gt 1) {
            $args += $parts[1..($parts.Length - 1)]
        }
        $args += '-c'
        $args += "import sys; print('{}.{}.{}'.format(sys.version_info[0], sys.version_info[1], sys.version_info[2]))"
        $result = & $exe @args
        return [Version]$result.Trim()
    } catch {
        return $null
    }
}

function Ensure-Python([string]$PreferredExe) {
    $minimum = [Version]"3.12"

    if (Ensure-Command $PreferredExe) {
        $version = Get-PythonVersion $PreferredExe
        if ($version -and $version -ge $minimum) {
            Write-Step "found python executable '$PreferredExe' (version $version)"
            return $PreferredExe
        }
    }

    if (Ensure-Command "py") {
        $launcherCmd = "py -3.12"
        $version = Get-PythonVersion $launcherCmd
        if ($version -and $version -ge $minimum) {
            Write-Step "using Python via 'py -3.12' (version $version)"
            return $launcherCmd
        }
    }

    if (-not (Ensure-Command "winget")) {
        throw "Python 3.12+ not found. Install manually or install winget."
    }

    Write-Step "installing Python 3.12 via winget"
    Invoke-OrThrow "winget install -e --id Python.Python.3.12" "failed to install Python via winget"

    if (Ensure-Command "py") {
        $launcherCmd = "py -3.12"
        $version = Get-PythonVersion $launcherCmd
        if ($version -and $version -ge $minimum) {
            Write-Step "using Python via 'py -3.12' (version $version)"
            return $launcherCmd
        }
    }

    if (Ensure-Command $PreferredExe) {
        $version = Get-PythonVersion $PreferredExe
        if ($version -and $version -ge $minimum) {
            Write-Step "found python executable '$PreferredExe' (version $version)"
            return $PreferredExe
        }
    }

    throw "Python 3.12+ not available after installation."
}

function Get-VenvPython([string]$VenvPath) {
    return Join-Path $VenvPath "Scripts" | Join-Path -ChildPath "python.exe"
}

try {
    Write-Step "starting dependency installation"
    $PythonCommand = Ensure-Python $PythonExe

    $minimum = [Version]"3.12"
    if (-not (Test-Path $VenvPath)) {
        Write-Step "creating virtual environment at '$VenvPath'"
        Invoke-OrThrow "$PythonCommand -m venv `"$VenvPath`"" "failed to create virtual environment"
    } else {
        Write-Step "virtual environment '$VenvPath' already exists"
        $existingPython = Get-PythonVersion (Get-VenvPython $VenvPath)
        if (-not $existingPython -or $existingPython -lt $minimum) {
            Write-Step "existing virtual environment uses Python $existingPython; recreating"
            Remove-Item -Recurse -Force $VenvPath
            Invoke-OrThrow "$PythonCommand -m venv `"$VenvPath`"" "failed to create virtual environment"
        }
    }

    $VenvPython = Get-VenvPython $VenvPath
    if (-not (Test-Path $VenvPython)) {
        throw "virtual environment python executable not found at $VenvPython"
    }

    Write-Step "ensuring pip is available"
    Invoke-OrThrow "`"$VenvPython`" -m ensurepip --upgrade" "failed to bootstrap pip"

    Write-Step "upgrading pip"
    Invoke-OrThrow "`"$VenvPython`" -m pip install --upgrade pip" "failed to upgrade pip"

    Write-Step "installing project dependencies"
    Invoke-OrThrow "`"$VenvPython`" -m pip install -e ." "failed to install project dependencies"

    Write-Step "installing Playwright Chromium driver"
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw "failed to install Playwright Chromium (exit code $LASTEXITCODE)"
    }

    if ($InstallTesseract -and (Ensure-Command "winget")) {
        Write-Step "installing Tesseract OCR runtime via winget"
        Invoke-OrThrow "winget install -e --id UB-Mannheim.TesseractOCR" "failed to install Tesseract OCR"
    } elseif ($InstallTesseract) {
        Write-Step "winget not found; please install Tesseract OCR manually"
    } else {
        Write-Step "skipping Tesseract OCR installation (use -InstallTesseract to enable)"
    }

    Write-Step "installation complete"
    $activateCmd = "powershell -NoLogo -NoProfile -Command ""& `"$VenvPath\Scripts\Activate.ps1`""""
    Write-Host ("To activate the environment:`n  {0}" -f $activateCmd) -ForegroundColor Green
}
catch {
    Write-Host ("[install] ERROR: {0}" -f $_) -ForegroundColor Red
    exit 1
}
