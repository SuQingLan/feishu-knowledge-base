@echo off
cd /d "%~dp0"
python -X utf8 "%~dp0devinsync_feishu_chat_local.py" --full-history --print-paths
if errorlevel 1 echo Export stopped. Saved pages will be kept. See the error above.
pause
