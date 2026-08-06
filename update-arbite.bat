@echo off
setlocal

echo Updating arbite (global pipx install) from Y:\projects\arbite ...
echo.

python -m pipx install "Y:\projects\arbite" --force
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
