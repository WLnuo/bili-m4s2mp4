@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Set your Bilibili cache directory here (empty = this folder)
rem  e.g.  set "CACHE=D://bilibili//cache"
rem ============================================================
set "CACHE=%~dp0."

echo ============================================================
echo   Bilibili Cache Merge Tool
echo ============================================================
echo.
echo   Cache dir: %CACHE%
echo.

python "%~dp0bili_m4s2mp4.py" --root "%CACHE%" --pause off %*
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" (
    echo   Finished OK
) else (
    echo   Finished with exit code %RC%, see output above
)
echo   Log: %CACHE%\bili_m4s2mp4.log
echo ============================================================
echo.
pause
