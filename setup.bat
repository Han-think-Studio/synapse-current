@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SYNAPSE_ROOT=%~dp0"
for %%I in ("%SYNAPSE_ROOT%.\") do set "SYNAPSE_ROOT=%%~fI"
set "SYNAPSE_PAUSE=1"
if /I "%~1"=="--auto" set "SYNAPSE_PAUSE=0"

pushd "%SYNAPSE_ROOT%" >nul 2>&1
if errorlevel 1 goto root_error
if not exist "%SYNAPSE_ROOT%\pyproject.toml" goto root_error
if not exist "%SYNAPSE_ROOT%\requirements.txt" goto root_error

echo ===================================================
echo [Synapse mini] Portable setup
echo ===================================================
echo [INFO] Synapse root detected: %SYNAPSE_ROOT%
echo [INFO] Current directory is not used for path resolution.
echo.

set "PYTHON_LAUNCHER="
where python >nul 2>&1
if not errorlevel 1 (
    python --version 2^>^&1 | findstr /C:"Python 3.12." >nul
    if not errorlevel 1 set "PYTHON_LAUNCHER=python"
)
if defined PYTHON_LAUNCHER goto python_ready

where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 --version 2^>^&1 | findstr /C:"Python 3.12." >nul
    if not errorlevel 1 set "PYTHON_LAUNCHER=py -3.12"
)
if defined PYTHON_LAUNCHER goto python_ready

echo [ERROR] Python 3.12.x was not found on PATH or through the py launcher.
echo [ERROR] Install Python 3.12.x, then run this file again from any folder.
goto setup_error

:python_ready
echo [INFO] System Python detected: %PYTHON_LAUNCHER%

if not exist "%SYNAPSE_ROOT%\.venv\Scripts\python.exe" goto create_venv
"%SYNAPSE_ROOT%\.venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 goto create_venv
"%SYNAPSE_ROOT%\.venv\Scripts\python.exe" --version 2^>^&1 | findstr /C:"Python 3.12." >nul
if errorlevel 1 goto create_venv
goto venv_ready

:create_venv
if exist "%SYNAPSE_ROOT%\.venv" (
    echo [WARNING] Invalid or stale .venv detected. Recreating it...
    rmdir /s /q "%SYNAPSE_ROOT%\.venv"
)
echo [1/2] Creating virtual environment at %SYNAPSE_ROOT%\.venv ...
call %PYTHON_LAUNCHER% -m venv "%SYNAPSE_ROOT%\.venv"
if errorlevel 1 goto setup_error

:venv_ready
echo [INFO] Local Python detected: "%SYNAPSE_ROOT%\.venv\Scripts\python.exe"
echo [1/2] Virtual environment is ready.
echo [2/2] Installing the one declared runtime dependency...
"%SYNAPSE_ROOT%\.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto setup_error
"%SYNAPSE_ROOT%\.venv\Scripts\python.exe" -m pip install -r "%SYNAPSE_ROOT%\requirements.txt"
if errorlevel 1 goto setup_error

echo.
echo ===================================================
echo [SUCCESS] Synapse mini portable setup completed.
echo [INFO] Only "%SYNAPSE_ROOT%\.venv" was changed.
echo [INFO] Run run.bat or run-core.bat from any directory.
echo ===================================================
set "SYNAPSE_EXIT=0"
goto finish

:root_error
echo [ERROR] This launcher is not beside a valid Synapse mini root.
echo [ERROR] Expected pyproject.toml and requirements.txt under:
echo          %SYNAPSE_ROOT%
set "SYNAPSE_EXIT=1"
goto finish

:setup_error
echo [ERROR] Portable setup failed. The detected root was:
echo          %SYNAPSE_ROOT%
set "SYNAPSE_EXIT=1"

:finish
popd >nul 2>&1
if "%SYNAPSE_PAUSE%"=="1" pause
exit /b %SYNAPSE_EXIT%
