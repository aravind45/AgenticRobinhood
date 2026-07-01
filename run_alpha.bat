@echo off
:: run_alpha.bat
:: Launches the Project Alpha autonomous daemon.
:: Set ANTHROPIC_API_KEY before running, or add it to Windows Environment Variables.

cd /d "%~dp0"

:: Force UTF-8 output so Claude's emoji responses don't crash the log
set PYTHONIOENCODING=utf-8

:: Uncomment and fill in the line below if ANTHROPIC_API_KEY is not set globally:
:: set ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE

echo [%DATE% %TIME%] Starting Project Alpha daemon...
python daemon.py >> alpha_daemon.log 2>&1
