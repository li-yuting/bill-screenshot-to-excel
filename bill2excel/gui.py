"""账单长截图 → Excel：入口（GUI + 命令行 + 自检）。

用法::

    python gui.py                     # 打开图形界面
    python gui.py <图片文件夹>          # 无界面直接转换
    python gui.py --selftest          # 自检：纯函数断言 + 跑一遍当前目录并核对
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

import parse

def app_dir() -> Path:
    """程序所在目录。

    打包（PyInstaller）后 ``__file__`` 指向临时解包目录，必须改用 exe 所在目录，
    否则界面里默认打开的是一个随机的 _MEIxxxx 临时路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DEFAULT_HIGHLIGHT = "59, 69, 179, 199"
LogFn = Callable[[str], None]


def parse_highlight(text: str) -> set[float]:
    """把界面/命令行里的高亮金额文本解析成集合。

    Args:
        text: 形如 ``59, 69, 179, 199`` 的文本，支持中英文逗号、顿号、空格分隔。

    Returns:
        金额集合（按绝对值匹配）。

    Raises:
        ValueError: 含非数字项。
    """
    values: set[float] = set()
    for piece in re.split(r"[,，、\s]+", text.strip()):
        if piece:
            values.add(round(float(piece), 2))
    return values


def output_path(folder: Path, groups: list, explicit: Path | None = None) -> Path:
    """按账单年份区间生成输出文件名。"""
    if explicit:
        return explicit
    years = sorted({group.year for group in groups if group.year.isdigit()})
    span = f"{years[0]}-{years[-1]}" if years else "全部"
    return folder / f"账单明细_{span}.xlsx"


def run_pipeline(folder: Path, highlight: set[float], log: LogFn = print,
                 out: Path | None = None) -> Path:
    """完整流程：扫描去重 → OCR → 解析 → 合并校验 → 导出 Excel。

    Args:
        folder: 图片所在目录。
        highlight: 需要高亮的金额集合。
        log: 日志回调。
        out: 指定输出文件；默认落在图片目录下。

    Returns:
        生成的 Excel 路径。

    Raises:
        FileNotFoundError: 目录不存在或没有图片。
        PermissionError: 目标 Excel 被占用。
    """
    import excel  # 延迟导入：openpyxl / onnxruntime 加载要几秒，别卡住开窗
    import ocr

    shots = ocr.scan_images(folder)
    if not shots:
        raise FileNotFoundError(f"{folder} 下没有找到图片")
    file_count = len(shots) + sum(len(s.copies) for s in shots)
    if file_count > len(shots):
        log(f"发现 {file_count} 个图片文件，内容去重后 {len(shots)} 张唯一截图")
    else:
        log(f"发现 {len(shots)} 张截图")

    engine = ocr.create_engine()
    for index, shot in enumerate(shots, 1):
        log(f"[{index}/{len(shots)}] OCR {shot.path.name} …")
        ocr.ocr_shot(shot, engine, log)

    pages = [
        parse.parse_page(shot.items, shot.path.name, [c.name for c in shot.copies]) for shot in shots
    ]
    missing_summary = [p.source for p in pages if p.summary is None]
    if missing_summary:
        log(f"警告：这些图没识别到顶部汇总卡，将不做金额勾稽：{', '.join(missing_summary)}")

    groups = parse.build_groups(pages)
    log("")
    log("金额核对（明细合计 vs 页面汇总卡）")
    for group in groups:
        diff = group.expense_diff
        if diff is None:
            verdict = "无汇总卡，未勾稽"
        elif diff == 0:
            verdict = "一致"
        else:
            verdict = f"差 {diff:,.2f}"
        log(
            f"  {group.year}年  {len(group.records):>4} 笔  "
            f"支出 {group.expense:>12,.2f}（页面 {group.card_expense}）  "
            f"收入 {group.income:>10,.2f}（页面 {group.card_income}）  {verdict}"
        )
    duplicates = sum(group.duplicates for group in groups)
    if duplicates:
        log(f"  已去除重叠段重复交易 {duplicates} 笔")

    target = output_path(folder, groups, out)
    excel.write_workbook(target, groups, highlight, pages)
    log("")
    log(f"已生成：{target}")
    return target


class App:
    """tkinter 界面：选文件夹 → 后台线程跑流程 → 实时日志。"""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.running = False
        self.result: Path | None = None

        self.root = tk.Tk()
        self.root.title("账单长截图 → Excel")
        self.root.geometry("760x520")
        self.root.minsize(640, 420)

        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="图片文件夹").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=str(APP_DIR.parent))
        ttk.Entry(frame, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="浏览…", command=self.browse).grid(row=0, column=2)

        ttk.Label(frame, text="高亮金额").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.highlight_var = tk.StringVar(value=DEFAULT_HIGHLIGHT)
        ttk.Entry(frame, textvariable=self.highlight_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0)
        )
        self.start_button = ttk.Button(frame, text="开始转换", command=self.start)
        self.start_button.grid(row=1, column=2, pady=(6, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)

        self.log_view = ScrolledText(frame, height=18, font=("Consolas", 9), state="disabled")
        self.log_view.grid(row=3, column=0, columnspan=3, sticky="nsew")

        bottom = ttk.Frame(frame)
        bottom.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.open_button = ttk.Button(bottom, text="打开结果文件", command=self.open_result,
                                      state="disabled")
        self.open_button.pack(side="left")
        self.status = ttk.Label(bottom, text="就绪")
        self.status.pack(side="right")

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        self.root.after(100, self.poll)

    def browse(self) -> None:
        chosen = self.filedialog.askdirectory(initialdir=self.folder_var.get() or str(APP_DIR))
        if chosen:
            self.folder_var.set(chosen)

    def start(self) -> None:
        if self.running:
            return
        try:
            highlight = parse_highlight(self.highlight_var.get())
        except ValueError:
            self.log("高亮金额格式不对，示例：59, 69, 179, 199")
            return
        folder = Path(self.folder_var.get())
        self.running = True
        self.result = None
        self.start_button.config(state="disabled")
        self.open_button.config(state="disabled")
        self.progress.config(value=0)
        self.status.config(text="正在识别…")
        self.log(f"开始处理：{folder}")
        threading.Thread(target=self.work, args=(folder, highlight), daemon=True).start()

    def work(self, folder: Path, highlight: set[float]) -> None:
        try:
            target = run_pipeline(folder, highlight, self.log)
            self.queue.put(("done", target))
        except Exception as exc:  # 界面不能因为任何异常崩掉
            self.queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))

    def log(self, message: str) -> None:
        """线程安全：只往队列里塞，由主线程写控件。"""
        self.queue.put(("log", message))

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "done":
                    self.finish(payload)
                elif kind == "error":
                    self.append_log(payload)
                    self.running = False
                    self.start_button.config(state="normal")
                    self.status.config(text="失败")
                    self.messagebox.showerror("转换失败", str(payload).split("\n\n")[0])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def append_log(self, message: str) -> None:
        self.log_view.config(state="normal")
        self.log_view.insert("end", message + "\n")
        self.log_view.see("end")
        self.log_view.config(state="disabled")
        self.progress.config(value=min(100, self.progress["value"] + 100 / 12))

    def finish(self, target: Path) -> None:
        self.running = False
        self.result = target
        self.start_button.config(state="normal")
        self.open_button.config(state="normal")
        self.progress.config(value=100)
        self.status.config(text=f"完成：{target.name}")
        self.messagebox.showinfo("转换完成", f"已生成：\n{target}")

    def open_result(self) -> None:
        if self.result and self.result.exists():
            os.startfile(self.result)  # noqa: S606 - Windows 专用，打开 Excel

    def run(self) -> None:
        self.root.mainloop()


def selftest(folder: Path) -> int:
    """自检：先跑纯函数断言，再跑一遍真实目录并核对金额勾稽。

    Args:
        folder: 含账单截图的目录。

    Returns:
        0 通过，1 失败。
    """
    import excel
    import ocr

    parse.selftest()

    shots = ocr.scan_images(folder)
    if not shots:
        print(f"[skip] {folder} 下没有图片，仅完成纯函数自检")
        return 0

    engine = ocr.create_engine()
    for index, shot in enumerate(shots, 1):
        print(f"[{index}/{len(shots)}] OCR {shot.path.name}", flush=True)
        ocr.ocr_shot(shot, engine)
    pages = [
        parse.parse_page(shot.items, shot.path.name, [c.name for c in shot.copies]) for shot in shots
    ]
    groups = parse.build_groups(pages)

    target = output_path(folder, groups)
    excel.write_workbook(target, groups, parse_highlight(DEFAULT_HIGHLIGHT), pages)

    failures: list[str] = []
    print(f"[check] 唯一截图 {len(shots)} 张 / {len(groups)} 个账单期间")
    for group in groups:
        print(
            f"        {group.year}年  支出 {group.expense:>12,.2f}（页面 {group.card_expense}）"
            f"  差异 {group.expense_diff}  收入 {group.income:>10,.2f}（页面 {group.card_income}）"
        )
        if group.card_expense is None:
            failures.append(f"{group.name} 没识别出汇总卡")
        elif group.income_diff != 0:
            failures.append(f"{group.year}年 收入差 {group.income_diff:,.2f}")
    print(f"[check] 生成文件：{target}")

    if failures:
        print("[FAIL] " + "；".join(failures))
        return 1
    print("[PASS] parse.selftest + 收入勾稽 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账单长截图 → Excel")
    parser.add_argument("folder", nargs="?", help="图片文件夹；不传则打开图形界面")
    parser.add_argument("--highlight", default=DEFAULT_HIGHLIGHT, help="需要高亮的金额，逗号分隔")
    parser.add_argument("--out", help="输出 xlsx 路径")
    parser.add_argument("--selftest", action="store_true", help="跑自检（需要目录内有图片）")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(Path(args.folder).expanduser() if args.folder else APP_DIR.parent)

    highlight = parse_highlight(args.highlight)
    if args.folder:
        folder = Path(args.folder).expanduser()
        out = Path(args.out).expanduser() if args.out else None
        run_pipeline(folder, highlight, out=out)
        return 0

    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
