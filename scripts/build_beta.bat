@echo off
REM =============================================================================
REM build_beta.bat — Build and package GEMA for beta distribution
REM Run from the project root: scripts\build_beta.bat
REM Output: dist\gema-beta-v1\ (zip this folder and send to the friend)
REM =============================================================================

setlocal

set IMAGE_NAME=gema:beta-v1
set DIST_DIR=dist\gema-beta-v1

echo.
echo ============================================================
echo  GEMA Beta Builder
echo ============================================================
echo.

REM ── Build Docker image ────────────────────────────────────────────────────
echo [1/4] Building Docker image %IMAGE_NAME%...
docker build -t %IMAGE_NAME% .
if %ERRORLEVEL% neq 0 (
    echo ERROR: Docker build failed.
    pause
    exit /b 1
)
echo       Done.
echo.

REM ── Create dist folder ───────────────────────────────────────────────────
echo [2/4] Creating dist folder...
if exist %DIST_DIR% rmdir /s /q %DIST_DIR%
mkdir %DIST_DIR%
mkdir %DIST_DIR%\cookies
echo       Done.
echo.

REM ── Export Docker image ──────────────────────────────────────────────────
echo [3/4] Exporting image to tar (this may take a few minutes)...
docker save -o %DIST_DIR%\gema-beta-v1.tar %IMAGE_NAME%
if %ERRORLEVEL% neq 0 (
    echo ERROR: docker save failed.
    pause
    exit /b 1
)
echo       Done.
echo.

REM ── Copy companion files ─────────────────────────────────────────────────
echo [4/4] Copying launcher, compose file, and guide...
copy scripts\start_gema.bat        %DIST_DIR%\start_gema.bat        >nul
copy scripts\docker-compose.beta.yml %DIST_DIR%\docker-compose.yml  >nul
copy BETA_GUIDE.md                 %DIST_DIR%\BETA_GUIDE.md          >nul

REM Create a blank .env (wizard will fill it on first run)
echo. > %DIST_DIR%\.env

echo       Done.
echo.
echo ============================================================
echo  Package ready: %DIST_DIR%\
echo.
echo  Contents:
echo    gema-beta-v1.tar   ^(Docker image — ~3GB^)
echo    docker-compose.yml
echo    start_gema.bat     ^(friend double-clicks this^)
echo    BETA_GUIDE.md
echo    .env               ^(blank — wizard fills it^)
echo    cookies\           ^(empty — friend adds their own^)
echo.
echo  SCENARIO A ^(pre-configured^):
echo    Open %DIST_DIR%\.env and paste the friend's API keys,
echo    then zip and send. Wizard will not appear on first run.
echo.
echo  SCENARIO B ^(self-setup^):
echo    Leave .env blank. Zip and send.
echo    Friend runs start_gema.bat and the wizard guides them.
echo ============================================================
echo.
pause
