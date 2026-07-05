@echo off
rem 喝水小助手启动脚本：双击运行，无黑窗，进程独立（不依赖任何终端/会话）
cd /d %~dp0
start "" pythonw main.py
