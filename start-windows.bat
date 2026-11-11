@echo off
rem Double-click to install what cadsmith needs and open the studio.
title cadsmith studio
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv, the Python manager cadsmith uses...
  powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
)
where uv >nul 2>nul
if errorlevel 1 (
  echo Could not install uv. Take a screenshot of this window and send it.
  pause
  exit /b 1
)

echo Installing cadsmith. The first time takes a few minutes...
uv sync --python 3.12
if errorlevel 1 (
  echo The install failed. Take a screenshot of this window and send it.
  pause
  exit /b 1
)

where claude >nul 2>nul
if errorlevel 1 echo Claude Code is not installed, so the chat will not work until it is.

echo Opening the studio at http://127.0.0.1:8420 . Keep this window open; close it to stop.
start "" cmd /c "timeout /t 10 >nul & start http://127.0.0.1:8420"
uv run cadsmith web
pause
