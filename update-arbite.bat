@echo off
setlocal

rem Reinstall arbite from the current directory (the repo checkout) into the
rem global pipx environment. Run this from the repo root, e.g.:
rem     update-arbite.bat
echo Updating arbite (global pipx install) from %CD% ...
echo.

python -m pipx install "." --force
if errorlevel 1 (
    echo.
    echo Update failed - see errors above.
    pause
    exit /b 1
)

echo.
echo Done.
arbite --version

pause
