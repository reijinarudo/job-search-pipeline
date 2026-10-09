@echo off
REM Nightly job monitor. The cd matters: the script reads and writes
REM boards.json, seen.json and digest.md next to itself.
cd /d "%~dp0"
echo ---------------------------------------- >> monitor.log
echo Run started %DATE% %TIME% >> monitor.log
py job_monitor.py >> monitor.log 2>&1
echo Exit code %ERRORLEVEL% >> monitor.log
