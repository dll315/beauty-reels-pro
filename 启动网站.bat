@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 小姐姐放映厅 Pro ...
python server.py
pause
