@echo off
setlocal

rem Reinstall arbite from this repo checkout into the global pipx environment.
rem Safe to run from any directory: the repo root is derived from this script's
rem own location (this file lives in <repo>\scripts\), not from %CD%.
pushd "%~dp0.."

echo Updating arbite (global pipx install) from "%CD%" ...
echo.

python -m pipx install "." --force
if errorlevel 1 (
    echo.
    echo Update failed - see errors above.
    popd
    pause
    exit /b 1
)

echo.
echo Done.
arbite --version

popd
pause
