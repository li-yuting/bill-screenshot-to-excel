"""PyInstaller 打包脚本（打包控制台 app.py）。

用 Python 调 PyInstaller 而不是直接敲命令行：Windows 下通过 CreateProcessW 传参，
中文 exe 名（账单截图转Excel）不会因为 shell 编码问题变成乱码。

用法::

    build_exe.py            # 默认 onefile（本机能出）
    build_exe.py --onedir   # 目录形式（本机出不来，见下）

⚠️ 本机打不出 onedir：引导程序写进 ``build\\<名字>\\<名字>.exe`` 后会消失，
COLLECT 阶段报 ``WARNING: Ignoring non-existent resource ... meant to be collected as
<名字>.exe``，产物里只剩 ``_internal``（那一坨有 200+ MB，所以体积看着还挺大）。
疑似企业终端安全软件（亚信安全 SECOMN64/SECOCL64 等）静默清理。
**PyInstaller 退出码 0 也不代表产物存在** —— 所以下面必须显式判 ``target.exists()``。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_NAME = "账单截图转Excel"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = "--onedir" if "--onedir" in args else "--onefile"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",  # 不弹控制台窗口
        mode,
        "--name",
        EXE_NAME,
        "--icon",
        str(HERE / "app.ico"),
        # OCR 模型、onnxruntime 的原生 DLL 都在包数据里，必须整包收进来
        "--collect-all",
        "rapidocr_onnxruntime",
        "--collect-all",
        "onnxruntime",
        str(HERE / "app.py"),
    ]
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode != 0:
        print(f"[FAIL] PyInstaller 退出码 {result.returncode}")
        return result.returncode

    target = (
        HERE / "dist" / f"{EXE_NAME}.exe"
        if mode == "--onefile"
        else HERE / "dist" / EXE_NAME / f"{EXE_NAME}.exe"
    )
    # 退出码 0 不代表产物存在：onedir 的引导程序会被本机安全软件吞掉
    if not target.exists():
        print(f"[FAIL] PyInstaller 报了成功，但产物不存在：{target}")
        if mode == "--onedir":
            print("       本机打不出 onedir，请改用默认的 --onefile。")
        return 1

    print(f"[OK] 产物：{target}")
    print(f"[OK] 体积：{target.stat().st_size / 1024 / 1024:.0f} MB")
    print("[NEXT] 跑 smoke_test.py 冒烟（自检 + 确认 GUI 真起窗），别只看退出码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
