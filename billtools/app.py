"""账单截图转 Excel 控制台：启动台选账单类型 → 进入对应的识别工具。

命令行::

    python app.py                                  # 打开控制台（启动台）
    python app.py detail  "D:\\图片目录"             # 直接跑某个工具
    python app.py --selftest detail "D:\\图片目录"    # 自检：纯函数断言 + 跑一遍并核对
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_HIGHLIGHT = "59, 69, 179, 199"
LogFn = Callable[[str], None]
RunnerFn = Callable[..., Path]


def app_dir() -> Path:
    """程序所在目录。

    打包（PyInstaller）后 ``__file__`` 指向临时解包目录，必须改用 exe 所在目录，
    否则界面里默认打开的是一个随机的 _MEIxxxx 临时路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()


def parse_highlight(text: str) -> set[float]:
    """把界面里的高亮金额文本解析成集合。

    Args:
        text: 形如 ``59, 69, 179, 199``，支持中英文逗号、顿号、空格分隔。

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


def _scan(folder: Path, log: LogFn) -> list:
    """扫描目录并按内容去重，顺带把结果写进日志。"""
    import ocr

    shots = ocr.scan_images(folder)
    if not shots:
        raise FileNotFoundError(f"{folder} 下没有找到图片")
    file_count = len(shots) + sum(len(s.copies) for s in shots)
    log(f"发现 {file_count} 个图片文件，内容去重后 {len(shots)} 张唯一截图")
    return shots


def _ocr_all(shots: list, log: LogFn) -> None:
    import ocr

    engine = ocr.create_engine()
    for index, shot in enumerate(shots, 1):
        log(f"[{index}/{len(shots)}] OCR {shot.path.name} …")
        ocr.ocr_shot(shot, engine, log)


def run_statement(folder: Path, highlight: set[float], log: LogFn = print,
                  out: Path | None = None, shots: list | None = None) -> Path:
    """工具一：好分期账单长截图 → 一图一表 + 汇总。

    Args:
        folder: 截图目录。
        highlight: 需要高亮的金额。
        log: 日志回调。
        out: 指定输出文件；默认落在截图目录下。
        shots: 已经识别过的截图（自检时复用，避免重复 OCR）。

    Returns:
        生成的 Excel 路径。

    Raises:
        FileNotFoundError: 目录不存在或没有图片。
        PermissionError: 目标 Excel 被占用。
    """
    import excel
    import statement

    if shots is None:
        shots = _scan(folder, log)
        _ocr_all(shots, log)
    pages = [
        statement.parse_page(shot.items, shot.path.name, [c.name for c in shot.copies])
        for shot in shots
    ]
    missing_summary = [p.source for p in pages if p.summary is None]
    if missing_summary:
        log(f"警告：这些图没识别到顶部汇总卡，将不做金额勾稽：{', '.join(missing_summary)}")

    groups = statement.build_groups(pages)
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

    if out is None:
        years = sorted({group.year for group in groups if group.year.isdigit()})
        span = f"{years[0]}-{years[-1]}" if years else "全部"
        out = folder / f"账单明细_{span}.xlsx"
    excel.write_statement(out, groups, highlight, pages)
    log("")
    log(f"已生成：{out}")
    return out


def run_detail(folder: Path, highlight: set[float], log: LogFn = print,
               out: Path | None = None, shots: list | None = None) -> Path:
    """工具二：收支详情单页 → 明细 + 多维汇总。

    Args:
        folder: 截图目录。
        highlight: 需要高亮的金额。
        log: 日志回调。
        out: 指定输出文件；默认落在截图目录下。
        shots: 已经识别过的截图（自检时复用，避免重复 OCR）。

    Returns:
        生成的 Excel 路径。

    Raises:
        FileNotFoundError: 目录不存在或没有图片。
        ValueError: 一张都没解析出来。
        PermissionError: 目标 Excel 被占用。
    """
    import detail
    import excel

    if shots is None:
        shots = _scan(folder, log)
        _ocr_all(shots, log)

    details = []
    for shot in shots:
        try:
            details.append(
                detail.parse_page(shot.items, shot.path.name, [c.name for c in shot.copies])
            )
        except ValueError as exc:
            log(f"跳过（不是收支详情图？）：{exc}")

    details, repeats = detail.dedupe(details)
    if repeats:
        log(f"去重：{len(repeats)} 张截图与前面某张是同一笔交易，已合并")
        for copy_name, first in repeats.items():
            log(f"    {copy_name} 与 {first} 是同一笔")

    report = detail.summaries_of(details)
    checked = len(details) - len(report["unknown"])
    log("")
    log("自动校验")
    log(f"  交易金额与顶部金额一致：{checked}/{len(details)} 通过"
        f"，不一致 {len(report['inconsistent'])} 张")
    log(f"  字段缺失：{'无' if not report['missing'] else len(report['missing'])} 张")
    for source in report["inconsistent"]:
        log(f"    ! 金额不一致：{source}")
    for source, fields in report["missing"].items():
        log(f"    ! 字段缺失：{source}（缺 {'、'.join(fields)}）")
    log(f"  卡号 {'、'.join(report['cards']) or '—'}"
        f"　账户 {'、'.join(report['accounts']) or '—'}"
        f"　户名 {'、'.join(report['holders']) or '—'}")
    expense = round(sum(-d.signed_amount for d in details if (d.signed_amount or 0) < 0), 2)
    income = round(sum(d.signed_amount for d in details if (d.signed_amount or 0) > 0), 2)
    log(f"  合计 {len(details)} 笔：支出 {expense:,.2f} 元，收入 {income:,.2f} 元")

    if out is None:
        years = sorted({d.year for d in details if d.year.isdigit()})
        span = f"{years[0]}-{years[-1]}" if years else "全部"
        out = folder / f"收支详情明细_{span}.xlsx"
    duplicates = {copy.name: shot.path.name for shot in shots for copy in shot.copies}
    file_count = len(shots) + len(duplicates)
    excel.write_detail(out, details, highlight, file_count=file_count,
                       duplicates=duplicates, repeats=repeats)
    log("")
    log(f"已生成：{out}")
    return out


@dataclass(frozen=True)
class ToolSpec:
    """一个可选的识别工具。"""

    key: str
    name: str
    subtitle: str
    hint: str
    runner: RunnerFn


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="statement",
        name="好分期账单长截图",
        subtitle="多段长截图 · 一图一表 + 汇总",
        hint="识别顶部汇总卡、逐行金额与余额",
        runner=run_statement,
    ),
    ToolSpec(
        key="detail",
        name="收支详情单页",
        subtitle="每图一笔 · 明细 + 多维汇总",
        hint="识别 金额 / 时间 / 摘要 / 交易场所 等 8 字段",
        runner=run_detail,
    ),
)


def find_tool(key: str) -> ToolSpec:
    """按 key 找工具。

    Raises:
        KeyError: 没有这个工具。
    """
    for tool in TOOLS:
        if tool.key == key:
            return tool
    keys = "、".join(tool.key for tool in TOOLS)
    raise KeyError(f"未知工具 {key}，可选：{keys}")


CARD_BG = "#F7F8FA"
CARD_HOVER = "#EAF1FA"
CARD_BORDER = "#D5DAE2"
LOG_COLLAPSED = 320
LOG_EXPANDED = 600


class Console:
    """控制台窗口：启动台（选工具） + 操作页（跑转换）。"""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.spec: ToolSpec | None = None
        self.result: Path | None = None
        self.running = False
        self.log_open = False

        self.root = tk.Tk()
        self.root.title("账单截图转 Excel")
        self.root.minsize(720, LOG_COLLAPSED)
        self.root.geometry(f"820x{LOG_COLLAPSED}")

        self.container = ttk.Frame(self.root, padding=12)
        self.container.pack(fill="both", expand=True)
        self.container.columnconfigure(0, weight=1)
        self.container.rowconfigure(0, weight=1)

        self.launcher = ttk.Frame(self.container)
        self.workspace = ttk.Frame(self.container)
        self._build_launcher()
        self._build_workspace()
        self.show_launcher()

        self.root.after(100, self._poll)

    # ---------------- 启动台 ----------------

    def _build_launcher(self) -> None:
        tk, ttk = self.tk, self.ttk
        ttk.Label(self.launcher, text="选择要转换的账单类型",
                  font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self.launcher, text="两类截图版式不同，工具会按你的选择加载对应的识别规则",
                  foreground="#666666").grid(row=1, column=0, sticky="w", pady=(2, 14))

        cards = ttk.Frame(self.launcher)
        cards.grid(row=2, column=0, sticky="ew")
        for column, spec in enumerate(TOOLS):
            cards.columnconfigure(column, weight=1, uniform="card")
            self._make_card(cards, spec, column)
        self.launcher.columnconfigure(0, weight=1)

    def _make_card(self, parent, spec: ToolSpec, column: int) -> None:
        tk = self.tk
        frame = tk.Frame(parent, bg=CARD_BG, highlightthickness=1,
                         highlightbackground=CARD_BORDER, cursor="hand2", padx=14, pady=12)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 12) if column == 0 else 0)

        widgets = [frame]
        title = tk.Label(frame, text=spec.name, bg=CARD_BG, font=("Microsoft YaHei UI", 11, "bold"),
                         anchor="w")
        title.pack(fill="x")
        widgets.append(title)
        for text, size, color in ((spec.subtitle, 9, "#444444"), (spec.hint, 8, "#888888")):
            label = tk.Label(frame, text=text, bg=CARD_BG, fg=color, anchor="w",
                             font=("Microsoft YaHei UI", size), justify="left")
            label.pack(fill="x", pady=(6 if size == 9 else 4, 0))
            widgets.append(label)
        enter = tk.Label(frame, text="点击进入 →", bg=CARD_BG, fg="#1F6FB2", anchor="w",
                         font=("Microsoft YaHei UI", 9))
        enter.pack(fill="x", pady=(10, 0))
        widgets.append(enter)

        def hover(on: bool) -> None:
            color = CARD_HOVER if on else CARD_BG
            for widget in widgets:
                widget.configure(bg=color)

        def click(_event=None) -> None:
            self.choose(spec)

        for widget in widgets:
            widget.bind("<Enter>", lambda _e: hover(True))
            widget.bind("<Leave>", lambda _e: hover(False))
            widget.bind("<Button-1>", click)

    def choose(self, spec: ToolSpec) -> None:
        """选中某个工具，切到操作页（每次都从启动台进来，不记忆上次选择）。"""
        self.spec = spec
        self.result = None
        self.current_label.configure(text=spec.name)
        self.open_button.configure(state="disabled")
        self.status.configure(text="就绪")
        self.progress.configure(value=0)
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")
        self.show_workspace()

    def show_launcher(self) -> None:
        self.workspace.grid_remove()
        self.launcher.grid(row=0, column=0, sticky="nsew")
        self.set_log(False)  # 回启动台时把日志收起来，窗口跟着压小

    def show_workspace(self) -> None:
        self.launcher.grid_remove()
        self.workspace.grid(row=0, column=0, sticky="nsew")

    # ---------------- 操作页 ----------------

    def _build_workspace(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = self.workspace
        frame.columnconfigure(1, weight=1)

        ttk.Button(frame, text="← 换一种账单类型", width=18,
                   command=self.show_launcher).grid(row=0, column=0, sticky="w")
        self.current_label = ttk.Label(frame, text="", font=("Microsoft YaHei UI", 11, "bold"))
        self.current_label.grid(row=0, column=1, sticky="w", padx=10)

        ttk.Label(frame, text="截图文件夹").grid(row=1, column=0, sticky="w", pady=(14, 0))
        self.folder_var = tk.StringVar(value=str(APP_DIR.parent))
        ttk.Entry(frame, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=10, pady=(14, 0))
        ttk.Button(frame, text="浏览…", command=self.browse).grid(row=1, column=2, pady=(14, 0))

        ttk.Label(frame, text="高亮金额").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.highlight_var = tk.StringVar(value=DEFAULT_HIGHLIGHT)
        ttk.Entry(frame, textvariable=self.highlight_var, width=24).grid(
            row=2, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Label(frame, text="命中金额的整行标黄", foreground="#888888").grid(
            row=2, column=2, sticky="w", pady=(10, 0))

        bar = ttk.Frame(frame)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(16, 10))
        self.start_button = ttk.Button(bar, text="开始转换", command=self.start)
        self.start_button.pack(side="left")
        self.open_button = ttk.Button(bar, text="打开结果文件", state="disabled",
                                      command=self.open_result)
        self.open_button.pack(side="left", padx=10)
        self.log_button = ttk.Button(bar, text="显示日志 ▾", width=12, command=self.toggle_log)
        self.log_button.pack(side="right")
        self.status = ttk.Label(bar, text="就绪", foreground="#666666")
        self.status.pack(side="right", padx=12)

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew")

        self.log_wrap = ttk.Frame(frame)
        self.log_wrap.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        self.log_wrap.grid_remove()
        frame.rowconfigure(5, weight=1)
        from tkinter.scrolledtext import ScrolledText

        self.log_view = ScrolledText(self.log_wrap, height=14, font=("Consolas", 9),
                                     state="disabled")
        self.log_view.pack(fill="both", expand=True)

    def toggle_log(self) -> None:
        """折叠/展开日志区。"""
        self.set_log(not self.log_open)

    def set_log(self, opened: bool) -> None:
        """日志区展开时把窗口拉高，折叠时压小（打开控制台默认就是折叠）。"""
        self.log_open = opened
        if opened:
            self.log_wrap.grid()
            self.log_button.configure(text="收起日志 ▴")
            self.root.geometry(f"820x{LOG_EXPANDED}")
        else:
            self.log_wrap.grid_remove()
            self.log_button.configure(text="显示日志 ▾")
            self.root.geometry(f"820x{LOG_COLLAPSED}")

    def browse(self) -> None:
        chosen = self.filedialog.askdirectory(initialdir=self.folder_var.get() or str(APP_DIR))
        if chosen:
            self.folder_var.set(chosen)

    def start(self) -> None:
        if self.running or self.spec is None:
            return
        try:
            highlight = parse_highlight(self.highlight_var.get())
        except ValueError:
            self.messagebox.showwarning("格式不对", "高亮金额示例：59, 69, 179, 199")
            return
        folder = Path(self.folder_var.get())
        if not folder.is_dir():
            self.messagebox.showwarning("路径不对", f"找不到文件夹：\n{folder}")
            return

        self.running = True
        self.result = None
        self.start_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.configure(text="正在识别…")
        self.append_log(f"开始处理：{folder}")
        self.append_log(f"使用工具：{self.spec.name}")
        threading.Thread(target=self._work, args=(self.spec, folder, highlight), daemon=True).start()

    def _work(self, spec: ToolSpec, folder: Path, highlight: set[float]) -> None:
        try:
            self.queue.put(("done", spec.runner(folder, highlight, self.log)))
        except Exception as exc:  # 界面不能因为任何异常崩掉
            self.queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))

    def log(self, message: str) -> None:
        """线程安全：只往队列里塞，由主线程写控件。"""
        self.queue.put(("log", message))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self.append_log(payload)
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status.configure(text="失败")
                    self.messagebox.showerror("转换失败", str(payload).split("\n\n")[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def append_log(self, message: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", message + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")
        self.progress.configure(value=min(100, self.progress["value"] + 2.5))

    def _finish(self, target: Path) -> None:
        self.running = False
        self.result = target
        self.start_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.progress.configure(value=100)
        self.status.configure(text=f"完成：{target.name}")
        if not self.log_open:
            self.set_log(True)  # 跑完自动展开日志，让人能看到核对结果
        self.messagebox.showinfo("转换完成", f"已生成：\n{target}")

    def open_result(self) -> None:
        if self.result and self.result.exists():
            os.startfile(self.result)  # noqa: S606 - Windows 专用，打开 Excel

    def run(self) -> None:
        self.root.mainloop()


def selftest(tool_key: str, folder: Path) -> int:
    """自检：先跑纯函数断言，再跑一遍真实目录并打印校验结果。

    Args:
        tool_key: 工具 key。
        folder: 截图目录。

    Returns:
        0 通过，1 失败。
    """
    import detail
    import ocr
    import statement

    statement.selftest()
    detail.selftest()
    spec = find_tool(tool_key)

    shots = ocr.scan_images(folder)
    if not shots:
        print(f"[skip] {folder} 下没有图片，仅完成纯函数自检")
        return 0

    failures: list[str] = []
    engine = ocr.create_engine()
    for index, shot in enumerate(shots, 1):
        print(f"[{index}/{len(shots)}] OCR {shot.path.name}", flush=True)
        ocr.ocr_shot(shot, engine)

    if tool_key == "detail":
        details = [
            detail.parse_page(s.items, s.path.name, [c.name for c in s.copies]) for s in shots
        ]
        details, repeats = detail.dedupe(details)
        report = detail.summaries_of(details)
        if report["inconsistent"]:
            failures.append(f"金额不一致：{report['inconsistent']}")
        if report["missing"]:
            failures.append(f"字段缺失：{list(report['missing'])}")
        print(f"[check] 唯一截图 {len(shots)} 张 → 同一笔合并后 {len(details)} 笔"
              f"（重复截图 {len(repeats)} 张）")
        print(f"[check] 交易金额与顶部金额一致 "
              f"{len(details) - len(report['unknown'])}/{len(details)}")
        print(f"[check] 支出合计 {sum(abs(d.signed_amount or 0) for d in details):,.2f} 元")
    else:
        pages = [
            statement.parse_page(s.items, s.path.name, [c.name for c in s.copies]) for s in shots
        ]
        groups = statement.build_groups(pages)
        for group in groups:
            print(f"        {group.year}年  支出 {group.expense:>12,.2f}"
                  f"（页面 {group.card_expense}）差异 {group.expense_diff}"
                  f"  收入 {group.income:>10,.2f}（页面 {group.card_income}）")
            if group.card_expense is None:
                failures.append(f"{group.name} 没识别出汇总卡")
            elif group.income_diff != 0:
                failures.append(f"{group.year}年 收入差 {group.income_diff:,.2f}")

    target = spec.runner(folder, parse_highlight(DEFAULT_HIGHLIGHT), shots=shots)
    print(f"[check] 生成文件：{target}")
    if failures:
        print("[FAIL] " + "；".join(failures))
        return 1
    print("[PASS] 纯函数自检 + 金额校验 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账单截图转 Excel 控制台")
    parser.add_argument("tool", nargs="?", choices=[tool.key for tool in TOOLS],
                        help="工具：statement=好分期长截图，detail=收支详情单页")
    parser.add_argument("folder", nargs="?", help="截图目录；不给则打开控制台")
    parser.add_argument("--highlight", default=DEFAULT_HIGHLIGHT, help="高亮金额，逗号分隔")
    parser.add_argument("--out", help="输出 xlsx 路径")
    parser.add_argument("--selftest", action="store_true", help="跑自检（需要目录内有图片）")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(args.tool or "detail", Path(args.folder).expanduser()
                        if args.folder else APP_DIR.parent)

    if args.tool and args.folder:
        spec = find_tool(args.tool)
        out = Path(args.out).expanduser() if args.out else None
        spec.runner(Path(args.folder).expanduser(), parse_highlight(args.highlight), out=out)
        return 0

    Console().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
