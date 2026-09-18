"""PyInstaller 打包脚本（打包控制台 console.py）。

用 Python 调 PyInstaller 而不是直接敲命令行：Windows 下通过 CreateProcessW 传参，
中文 exe 名（账单转表格）不会因为 shell 编码问题变成乱码。

用法::

    build_exe.py            # 默认 onedir（启动快）
    build_exe.py --onefile  # 单文件（便于拷贝，冷启动慢）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_NAME = "账单转表格"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mode = "--onefile" if "--onefile" in args else "--onedir"

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
        str(HERE / "console.py"),
    ]
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=HERE, check=False)
    if result.returncode != 0:
        print(f"[FAIL] PyInstaller 退出码 {result.returncode}")
        return result.returncode

    if mode == "--onefile":
        target = HERE / "dist" / f"{EXE_NAME}.exe"
    else:
        target = HERE / "dist" / EXE_NAME / f"{EXE_NAME}.exe"
    print(f"[OK] 产物：{target}")
    print(f"[OK] 体积：{sum(f.stat().st_size for f in target.parent.rglob('*') if f.is_file()) / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
