@echo off
REM =============================================================================
REM start_gema.bat — Launch GEMA (beta)
REM Double-click this file to start GEMA.
REM Requires: Docker Desktop running (docker.com/products/docker-desktop)
REM =============================================================================

echo.
echo ============================================================
echo  Starting GEMA...
echo ============================================================
echo.

REM ── Check Docker is running ──────────────────────────────────────────────
docker info >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Docker Desktop is not running.
    echo.
    echo Please:
    echo   1. Open Docker Desktop from your Start Menu
    echo   2. Wait for the whale icon in the taskbar to stop animating
    echo   3. Double-click start_gema.bat again
    echo.
    pause
    exit /b 1
)

REM ── Load image if not already loaded ─────────────────────────────────────
docker image inspect gema:beta-v1 >nul 2>&1
if %ERRORLEVEL% neq 0 (
    if not exist gema-beta-v1.tar (
        echo ERROR: gema-beta-v1.tar not found next to this file.
        echo Make sure start_gema.bat and gema-beta-v1.tar are in the same folder.
        pause
        exit /b 1
    )
    echo Loading GEMA image (first run — takes 2-3 minutes)...
    docker load -i gema-beta-v1.tar
    if %ERRORLEVEL% neq 0 (
        echo ERROR: Failed to load image.
        pause
        exit /b 1
    )
    echo Image loaded.
    echo.
)

REM ── Start GEMA ───────────────────────────────────────────────────────────
echo Starting GEMA service...
docker compose up -d
if %ERRORLEVEL% neq 0 (
    echo ERROR: docker compose up failed.
    pause
    exit /b 1
)

REM ── Wait a moment then open browser ──────────────────────────────────────
echo Waiting for GEMA to start...
timeout /t 4 /nobreak >nul
start http://localhost:8501

echo.
echo ============================================================
echo  GEMA is running at: http://localhost:8501
echo.
echo  To stop GEMA:  docker compose down
echo  To update:     Ask Diego for a new package.
echo ============================================================
echo.
