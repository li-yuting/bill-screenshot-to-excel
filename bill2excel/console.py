"""共用控制台：先选账单类型，再转换。

一个窗口两个视图：

* 启动台：卡片式入口，每次打开都显示（不记忆上次选择）
* 操作页：截图文件夹 / 高亮金额 / 开始转换 / 进度 / 可折叠日志

两个工具共用操作页外壳，只有参数与日志文案不同；各自的识别逻辑分别在
``gui.run_pipeline``（长截图）和 ``detail.run``（收支详情单页）里。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import detail
import gui

APP_DIR = gui.app_dir()
FONT = "Microsoft YaHei UI"
PAGE_BG = "#ffffff"
LOG_BG = "#f7f7f5"
TEXT = "#22211f"
TEXT2 = "#6b6a66"
TEXT3 = "#9a9894"
BORDER = "#dcdcda"
ACCENT = "#185FA5"
ACCENT_BG = "#E6F1FB"
SIZE_LAUNCHER = "780x380"
SIZE_TOOL_COMPACT = "780x300"
SIZE_TOOL_EXPANDED = "780x620"

Runner = Callable[..., Path]


@dataclass(frozen=True)
class Tool:
    """一个账单类型对应的一套识别逻辑。"""

    key: str
    name: str
    subtitle: str
    hint: str
    runner: Runner


def build_tools() -> list[Tool]:
    """工具注册表：加新账单类型只要往这里加一条。"""
    return [
        Tool(
            key="long",
            name="好分期账单长截图",
            subtitle="多段长截图 · 一图一表 + 汇总",
            hint="识别顶部汇总卡、逐行金额与余额",
            runner=gui.run_pipeline,
        ),
        Tool(
            key="detail",
            name="收支详情单页",
            subtitle="每图一笔 · 明细 + 多维汇总",
            hint="识别 金额 / 时间 / 摘要 / 交易场所 等 8 字段",
            runner=detail.run,
        ),
    ]


def bind_click(widget, callback) -> None:
    """给控件及其所有子控件绑定左键点击（tkinter 没有卡片控件，只能自己拼）。"""
    widget.bind("<Button-1>", lambda _event: callback())
    for child in widget.winfo_children():
        bind_click(child, callback)


class Console:
    """控制台窗口。"""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.tools = build_tools()
        self.current: Tool | None = None
        self.running = False
        self.result: Path | None = None
        self.log_visible = False

        self.root = tk.Tk()
        self.root.title("账单截图转 Excel")
        self.root.configure(bg=PAGE_BG)
        self.root.geometry(SIZE_LAUNCHER)
        self.root.minsize(700, 280)

        self.body = tk.Frame(self.root, bg=PAGE_BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=14)
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)

        self._build_launcher(self.body)
        self._build_tool_page(self.body)
        self._show_launcher()
        self.root.after(100, self._poll)

    # ------------------------------------------------------------------ 启动台
    def _build_launcher(self, parent) -> None:
        tk = self.tk
        self.launcher = tk.Frame(parent, bg=PAGE_BG)
        self.launcher.columnconfigure(0, weight=1)

        tk.Label(
            self.launcher, text="选择要转换的账单类型", bg=PAGE_BG, fg=TEXT,
            font=(FONT, 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            self.launcher, text="两类截图版式不同，工具会按你的选择加载对应的识别规则",
            bg=PAGE_BG, fg=TEXT2, font=(FONT, 9),
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        cards = tk.Frame(self.launcher, bg=PAGE_BG)
        cards.grid(row=2, column=0, sticky="nsew")
        for column in range(len(self.tools)):
            cards.columnconfigure(column, weight=1)
        for column, tool in enumerate(self.tools):
            self._build_card(cards, tool, column)
        self.launcher.rowconfigure(2, weight=1)

    def _build_card(self, parent, tool: Tool, column: int) -> None:
        tk = self.tk
        card = tk.Frame(
            parent, bg=PAGE_BG, cursor="hand2",
            highlightbackground=BORDER, highlightthickness=1, bd=0,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 12 if column == 0 else 0), pady=2)

        icon = tk.Canvas(card, width=38, height=38, bg=ACCENT_BG, highlightthickness=0)
        icon.pack(anchor="w", pady=(12, 8), padx=12)
        for index in range(3):
            icon.create_rectangle(8, 11 + index * 6, 30 - index * 6, 14 + index * 6,
                                  fill=ACCENT, outline="")

        tk.Label(card, text=tool.name, bg=PAGE_BG, fg=TEXT, font=(FONT, 11, "bold")).pack(
            anchor="w", padx=12
        )
        tk.Label(card, text=tool.subtitle, bg=PAGE_BG, fg=TEXT2, font=(FONT, 9)).pack(
            anchor="w", padx=12, pady=(2, 0)
        )
        tk.Label(card, text=tool.hint, bg=PAGE_BG, fg=TEXT3, font=(FONT, 8), wraplength=300,
                 justify="left").pack(anchor="w", padx=12, pady=(6, 0))
        tk.Label(card, text="点击进入 →", bg=PAGE_BG, fg=ACCENT, font=(FONT, 9)).pack(
            anchor="w", padx=12, pady=(10, 12)
        )

        def enter(_event=None):
            card.configure(highlightbackground=ACCENT, highlightthickness=2)

        def leave(_event=None):
            card.configure(highlightbackground=BORDER, highlightthickness=1)

        card.bind("<Enter>", enter)
        card.bind("<Leave>", leave)
        bind_click(card, lambda: self._open_tool(tool.key))

    # ---------------------------------------------------------------- 操作页
    def _build_tool_page(self, parent) -> None:
        tk = self.tk
        from tkinter import ttk

        self.page = tk.Frame(parent, bg=PAGE_BG)
        self.page.columnconfigure(1, weight=1)

        back = tk.Label(self.page, text="← 换一种账单类型", bg=PAGE_BG, fg=ACCENT,
                        font=(FONT, 9), cursor="hand2")
        back.grid(row=0, column=0, columnspan=2, sticky="w")
        back.bind("<Button-1>", lambda _e: self._show_launcher())

        self.tool_title = tk.Label(self.page, text="", bg=PAGE_BG, fg=TEXT, font=(FONT, 12, "bold"))
        self.tool_title.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 12))

        tk.Label(self.page, text="截图文件夹", bg=PAGE_BG, fg=TEXT2, font=(FONT, 9)).grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        self.folder_var = tk.StringVar(value=str(APP_DIR.parent))
        tk.Entry(self.page, textvariable=self.folder_var, font=(FONT, 9)).grid(
            row=2, column=1, sticky="ew"
        )
        ttk.Button(self.page, text="浏览…", command=self._browse).grid(row=2, column=2, padx=(8, 0))

        tk.Label(self.page, text="高亮金额", bg=PAGE_BG, fg=TEXT2, font=(FONT, 9)).grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self.highlight_var = tk.StringVar(value=gui.DEFAULT_HIGHLIGHT)
        tk.Entry(self.page, textvariable=self.highlight_var, font=(FONT, 9), width=22).grid(
            row=3, column=1, sticky="w", pady=(8, 0)
        )
        tk.Label(self.page, text="命中金额的整行标黄", bg=PAGE_BG, fg=TEXT3, font=(FONT, 8)).grid(
            row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        actions = tk.Frame(self.page, bg=PAGE_BG)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 8))
        self.start_button = ttk.Button(actions, text="开始转换", command=self._start)
        self.start_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="打开结果文件", command=self._open_result,
                                      state="disabled")
        self.open_button.pack(side="left", padx=(8, 0))
        self.status = tk.Label(actions, text="就绪", bg=PAGE_BG, fg=TEXT2, font=(FONT, 9))
        self.status.pack(side="right")

        self.progress = ttk.Progressbar(self.page, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew")

        self.log_toggle = tk.Label(self.page, text="▸ 显示日志", bg=PAGE_BG, fg=ACCENT,
                                   font=(FONT, 9), cursor="hand2")
        self.log_toggle.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.log_toggle.bind("<Button-1>", lambda _e: self._toggle_log())

        self.log_frame = tk.Frame(self.page, bg=PAGE_BG)
        self.log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        self.page.rowconfigure(7, weight=1)
        from tkinter.scrolledtext import ScrolledText

        self.log_view = ScrolledText(
            self.log_frame, height=14, font=("Consolas", 9), bg=LOG_BG, fg=TEXT2,
            relief="flat", highlightthickness=1, highlightbackground=BORDER, state="disabled",
        )
        self.log_view.grid(row=0, column=0, sticky="nsew")
        self.log_frame.grid_remove()  # 默认折叠

    # ---------------------------------------------------------------- 视图切换
    def _show_launcher(self) -> None:
        self.page.grid_remove()
        self.launcher.grid(row=0, column=0, sticky="nsew")
        self.root.geometry(SIZE_LAUNCHER)

    def _open_tool(self, key: str) -> None:
        self.current = next(tool for tool in self.tools if tool.key == key)
        self.tool_title.configure(text=self.current.name)
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.configure(text="就绪")
        self.result = None
        self.open_button.configure(state="disabled")
        self.launcher.grid_remove()
        self.page.grid(row=0, column=0, sticky="nsew")
        self.root.geometry(SIZE_TOOL_COMPACT)

    def _toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.grid()
            self.log_toggle.configure(text="▾ 收起日志")
            self.root.geometry(SIZE_TOOL_EXPANDED)
        else:
            self.log_frame.grid_remove()
            self.log_toggle.configure(text="▸ 显示日志")
            self.root.geometry(SIZE_TOOL_COMPACT)

    # ---------------------------------------------------------------- 动作
    def _browse(self) -> None:
        chosen = self.filedialog.askdirectory(initialdir=self.folder_var.get() or str(APP_DIR))
        if chosen:
            self.folder_var.set(chosen)

    def _start(self) -> None:
        if self.running or self.current is None:
            return
        try:
            highlight = gui.parse_highlight(self.highlight_var.get())
        except ValueError:
            self.messagebox.showwarning("高亮金额", "格式不对，示例：59, 69, 179, 199")
            return
        folder = Path(self.folder_var.get())
        if not folder.is_dir():
            self.messagebox.showwarning("截图文件夹", f"目录不存在：\n{folder}")
            return
        self.running = True
        self.result = None
        self.start_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.status.configure(text="正在处理…")
        self._log(f"开始处理：{folder}")
        threading.Thread(
            target=self._work, args=(self.current.runner, folder, highlight), daemon=True
        ).start()

    def _work(self, runner: Runner, folder: Path, highlight: set[float]) -> None:
        try:
            self.queue.put(("done", runner(folder, highlight, self._log_from_worker)))
        except Exception as exc:  # 界面不能因为任何异常崩掉
            self.queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))

    def _log(self, message: str) -> None:
        self.queue.put(("log", message))

    def _log_from_worker(self, message: str) -> None:
        """传给工具的日志回调（工具会带 \n 拼接多行）。"""
        for line in str(message).splitlines() or [""]:
            self._log(line)

    # ---------------------------------------------------------------- 事件循环
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._append_log(payload)
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status.configure(text="失败")
                    self.messagebox.showerror("处理失败", str(payload).split("\n\n")[0])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append_log(self, message: str) -> None:
        step = re.match(r"\[(\d+)/(\d+)\]", message)
        if step:
            done, total = int(step.group(1)), int(step.group(2))
            self.progress.configure(value=min(99, done / max(total, 1) * 100))
            self.status.configure(text=f"正在识别 {done}/{total}…")
        self.log_view.configure(state="normal")
        self.log_view.insert("end", message + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _finish(self, target: Path) -> None:
        self.running = False
        self.result = target
        self.start_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.progress.configure(value=100)
        self.status.configure(text=f"完成：{target.name}")
        self.messagebox.showinfo("转换完成", f"已生成：\n{target}")

    def _open_result(self) -> None:
        if self.result and self.result.exists():
            os.startfile(self.result)  # noqa: S606 - Windows 专用，交给系统默认程序打开

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if "--selftest" in sys.argv:
        detail.selftest()
        return 0
    Console().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
