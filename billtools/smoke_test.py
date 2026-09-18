# -*- coding: utf-8 -*-
"""打包产物冒烟测试：跑 exe 自检 + 确认 GUI 真的起窗（EnumWindows 查窗口标题）。

从 `_转换过程文件\verify_billtools_exe.py` 迁移而来，并把写死的 exe 路径改成命令行参数。

用法:
    python smoke_test.py                          # 默认测 ..\\billtools\\dist\\账单截图转Excel.exe
    python smoke_test.py D:\\path\\to\\xxx.exe      # 测指定产物

退出码 0 = 自检过 + GUI 起窗；1 = 任一环节失败。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_EXE = Path(__file__).resolve().parent / "dist" / "账单截图转Excel.exe"
EXE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_EXE
SMOKE = Path(os.environ.get("TEMP", ".")) / "billtools_smoke"
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

if not EXE.exists():                      # 打包脚本退出码 0 不代表产物存在，先卡这一道
    print(f"找不到产物: {EXE}")
    sys.exit(1)
print(f"exe 存在={EXE.exists()}  体积={EXE.stat().st_size / 1024 / 1024:.0f} MB\n")

print(f"=== exe --selftest detail {SMOKE} ===", flush=True)
started = time.time()
result = subprocess.run(
    [str(EXE), "--selftest", "detail", str(SMOKE)],
    capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=900,
)
print(result.stdout.strip())
print(f"退出码: {result.returncode}  耗时 {time.time() - started:.0f} 秒")
produced = sorted(SMOKE.glob("收支详情明细_*.xlsx"))
print("产物:", produced[-1].name if produced else "（没生成）")

# GUI：起窗后枚举顶层窗口，确认标题出现（比“进程还活着”更硬的证据）
user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def titles() -> list[str]:
    found: list[str] = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                found.append(buf.value)
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found


print("\n=== GUI 起窗测试（onefile 冷启动要解包，等 30 秒）===", flush=True)
proc = subprocess.Popen([str(EXE)], env=env)
hit = ""
for _ in range(15):
    time.sleep(2)
    if proc.poll() is not None:
        print("进程已退出，退出码", proc.returncode)
        break
    hit = next((t for t in titles() if "账单截图转" in t), "")
    if hit:
        break
print(f"找到窗口: {hit or '（没找到）'}")
if proc.poll() is None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("已结束 GUI 进程")
sys.exit(0 if (result.returncode == 0 and hit) else 1)
