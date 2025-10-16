@echo off
setlocal
set VENV=.venv
set PYTHON_EXE=%VENV%\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    echo [launcher] Virtual environment not found. Please run install_dependencies.ps1 first.
    exit /b 1
)

"%PYTHON_EXE%" src\orchestrator.py
if errorlevel 1 (
    echo [launcher] Orchestrator exited with error code %errorlevel%.
    pause
    exit /b %errorlevel%
)

endlocal
