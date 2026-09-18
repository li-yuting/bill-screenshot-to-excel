@echo off
rem 双击启动控制台（先选账单类型，再转换）
rem 用带 tkinter 的独立环境 + pythonw，不弹黑框
setlocal
set "PY=D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\pythonw.exe"
if not exist "%PY%" (
    echo [ERROR] 找不到运行环境：%PY%
    echo         请按 README 的先决条件创建环境并安装依赖。
    pause
    exit /b 1
)
start "" "%PY%" "%~dp0console.py"
