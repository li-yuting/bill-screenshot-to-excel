"""工具2：银行 App「收支详情」单页截图 → 明细 + 多维汇总。

一张截图 = 一笔交易。字段靠「左标签 → 同行右侧值」配对，并用
「交易金额 == |顶部金额|」做逐笔自校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型标注，避免运行时依赖 OCR 栈
    from ocr import Item

FIELD_LABELS = (
    "交易卡号",
    "交易账户",
    "交易户名",
    "交易时间",
    "业务摘要",
    "交易场所",
    "交易金额",
)
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")
NUMBER_RE = re.compile(r"-?[\d,]+\.\d{2}")
DATETIME_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2})\s*(\d{1,2}:\d{2}(?::\d{2})?)")
LABEL_ROW_TOLERANCE = 25
TOP_ZONE_Y = 400

_STYLE = {
    "border": "BFBFBF",
    "head_fill": "1F4E79",
    "head_font": "FFFFFF",
    "block_fill": "DDEBF7",
    "block_font": "1F4E79",
    "ok_fill": "E2EFDA",
    "ok_font": "375623",
    "bad_fill": "FFC7CE",
    "bad_font": "9C0006",
    "hit_fill": "FFD400",
    "hit_font": "7F3F00",
    "money": "#,##0.00",
    "center": "center",
    "right": "right",
    "left": "left",
}


@dataclass
class Entry:
    """一笔交易（一张截图）。"""

    source: str
    copies: list[str] = field(default_factory=list)
    amount: float = 0.0
    balance: float | None = None
    fields: dict[str, str] = field(default_factory=dict)
    date: str = ""
    time: str = ""
    issues: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.fields.get("业务摘要", "")

    @property
    def place(self) -> str:
        return self.fields.get("交易场所", "")

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else "未知"

    @property
    def month(self) -> str:
        return self.date[:7] if self.date else "未知"

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)

    @property
    def amount_ok(self) -> bool:
        """「交易金额」是否等于顶部金额的绝对值。"""
        raw = self.fields.get("交易金额", "")
        return bool(raw) and abs(_value(raw) - self.abs_amount) <= 0.005

    @property
    def fields_ok(self) -> bool:
        """7 个字段是否齐全。"""
        return all(label in self.fields for label in FIELD_LABELS)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _value(text: str) -> float:
    return float(text.replace(",", ""))


def parse_entry(items: Sequence["Item"], source: str, copies: Sequence[str] = ()) -> Entry:
    """解析一张「收支详情」截图。

    Args:
        items: 该图的 OCR 文字块。
        source: 代表文件名。
        copies: 内容相同的副本文件名。

    Returns:
        一笔交易；解析不到的内容留空并记录在 ``issues`` 里。
    """
    entry = Entry(source=source, copies=list(copies))
    rows = sorted(items, key=lambda it: it.y)

    header = next((it for it in rows if "余额" in _norm(it.t)), None)
    if header:
        found = NUMBER_RE.search(_norm(header.t))
        entry.balance = _value(found.group(0)) if found else None

    top = [it for it in rows if it.y < TOP_ZONE_Y and AMOUNT_RE.fullmatch(_norm(it.t))]
    if top:
        entry.amount = _value(_norm(max(top, key=lambda it: it.h).t))
    else:
        entry.issues.append("未识别到顶部金额")

    for label in FIELD_LABELS:
        holder = next((it for it in rows if label in _norm(it.t)), None)
        if holder is None:
            entry.issues.append(f"缺字段：{label}")
            continue
        same_row = [
            it for it in rows if abs(it.y - holder.y) <= LABEL_ROW_TOLERANCE and it.x > holder.x + 100
        ]
        if not same_row:
            entry.issues.append(f"缺字段：{label}")
            continue
        entry.fields[label] = _norm(max(same_row, key=lambda it: it.x).t)

    stamp = next((_norm(it.t) for it in rows if DATETIME_RE.search(_norm(it.t))), "")
    matched = DATETIME_RE.search(stamp)
    if matched:
        entry.date = matched.group(1)
        entry.time = matched.group(2)
    else:
        entry.issues.append("未识别到交易时间")

    raw_amount = entry.fields.get("交易金额", "")
    if not raw_amount:
        pass
    elif abs(_value(raw_amount) - entry.abs_amount) > 0.005:
        entry.issues.append(f"交易金额({raw_amount}) 与顶部金额({entry.amount:.2f}) 不一致")
    return entry


def _total(entries: Iterable[Entry]) -> float:
    return round(sum(e.abs_amount for e in entries), 2)


def _group(entries: Sequence[Entry], key) -> list[tuple[str, int, float]]:
    buckets: dict[str, list[Entry]] = {}
    for entry in entries:
        buckets.setdefault(key(entry), []).append(entry)
    rows = [(name, len(items), _total(items)) for name, items in buckets.items()]
    rows.sort(key=lambda row: row[0])
    return rows


@dataclass
class Report:
    """一批截图的汇总结果。"""

    scanned_files: int
    entries: list[Entry]
    duplicates: int
    by_year: list[tuple[str, int, float]]
    by_month: list[tuple[str, int, float]]
    by_amount: list[tuple[str, int, float]]
    by_kind: list[tuple[str, int, float]]
    by_place: list[tuple[str, int, float]]
    distinct: dict[str, set[str]]

    @property
    def total(self) -> float:
        return _total(self.entries)

    @property
    def issues(self) -> list[Entry]:
        return [e for e in self.entries if e.issues]

    @property
    def period(self) -> str:
        dates = sorted(e.date for e in self.entries if e.date)
        return f"{dates[0]} 至 {dates[-1]}" if dates else "未知"


def build_report(entries: Sequence[Entry], scanned_files: int, duplicates: int) -> Report:
    """把逐笔结果汇总成多维度报表。

    Args:
        entries: 去重后的交易（一张唯一截图一笔）。
        scanned_files: 目录里的图片文件总数（含副本）。
        duplicates: 被去掉的重复文件数。

    Returns:
        汇总结果，含按年/月/金额档/业务摘要/交易场所的分布与取值唯一性检查。
    """
    ordered = sorted(entries, key=lambda e: (e.date, e.time))
    by_amount = _group(ordered, lambda e: f"{e.abs_amount:g}")
    by_amount.sort(key=lambda row: float(row[0]))
    return Report(
        scanned_files=scanned_files,
        entries=ordered,
        duplicates=duplicates,
        by_year=_group(ordered, lambda e: e.year),
        by_month=_group(ordered, lambda e: e.month),
        by_amount=by_amount,
        by_kind=_group(ordered, lambda e: e.kind or "未知"),
        by_place=_group(ordered, lambda e: e.place or "未知"),
        distinct={
            label: {e.fields.get(label, "") for e in ordered if e.fields.get(label)}
            for label in ("交易卡号", "交易账户", "交易户名")
        },
    )


def run(folder: Path, highlight: set[float], log=print, out: Path | None = None) -> Path:
    """完整流程：扫描去重 → OCR → 解析 → 校验 → 导出。

    Args:
        folder: 截图目录。
        highlight: 需要高亮的金额（按绝对值匹配）。
        log: 日志回调。
        out: 指定输出文件；默认落在截图目录下。

    Returns:
        生成的 Excel 路径。

    Raises:
        FileNotFoundError: 目录不存在或没有图片。
    """
    import ocr

    shots = ocr.scan_images(folder)
    if not shots:
        raise FileNotFoundError(f"{folder} 下没有找到图片")
    file_count = len(shots) + sum(len(s.copies) for s in shots)
    log(f"发现 {file_count} 个图片文件，内容去重后 {len(shots)} 张唯一截图")

    engine = ocr.create_engine()
    for index, shot in enumerate(shots, 1):
        log(f"[{index}/{len(shots)}] OCR {shot.path.name}")
        ocr.ocr_shot(shot, engine)

    entries = [
        parse_entry(shot.items, shot.path.name, [c.name for c in shot.copies]) for shot in shots
    ]
    report = build_report(entries, file_count, file_count - len(shots))

    log("")
    log(f"交易明细（{len(report.entries)} 笔，支出 {report.total:,.2f} 元，{report.period}）")
    log(f"  校验：金额一致 {len(report.entries) - len(report.issues)}/{len(report.entries)}"
        f"　异常 {len(report.issues)} 笔")
    for entry in report.issues:
        log(f"    {entry.source}：{'；'.join(entry.issues)}")
    for label, values in report.distinct.items():
        if len(values) > 1:
            log(f"  注意：{label} 出现 {len(values)} 种取值 {sorted(values)}")
    for name, count, money in report.by_year:
        log(f"  {name} 年　{count:>3} 笔　{money:>10,.2f} 元")

    target = out or folder / f"收支详情明细_{_span(report)}.xlsx"
    write_workbook(target, report, highlight)
    log("")
    log(f"已生成：{target}")
    return target


def _span(report: Report) -> str:
    years = sorted({e.year for e in report.entries if e.year.isdigit()})
    return f"{years[0]}-{years[-1]}" if years else "全部"


def write_workbook(path: Path, report: Report, highlight: Iterable[float]) -> Path:
    """导出 Excel：明细 + 多维汇总。

    Args:
        path: 输出路径。
        report: 汇总结果。
        highlight: 需要高亮的金额。

    Returns:
        输出路径。

    Raises:
        PermissionError: 目标文件被占用。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    side = Side(style="thin", color=_STYLE["border"])
    border = Border(left=side, right=side, top=side, bottom=side)
    align = {name: Alignment(horizontal=_STYLE[name], vertical="center")
             for name in ("center", "right", "left")}
    hits = {round(float(v), 2) for v in highlight}
    money = _STYLE["money"]

    def head(ws, row: int, columns: Sequence[str]) -> None:
        for index, name in enumerate(columns, 1):
            cell = ws.cell(row=row, column=index, value=name)
            cell.border = border
            cell.alignment = align["center"]
            cell.fill = PatternFill("solid", fgColor=_STYLE["head_fill"])
            cell.font = Font(bold=True, color=_STYLE["head_font"], size=10)

    def block(ws, row: int, title: str, columns: Sequence[str]) -> int:
        cell = ws.cell(row=row, column=1, value=title)
        cell.font = Font(bold=True, size=10, color=_STYLE["block_font"])
        cell.fill = PatternFill("solid", fgColor=_STYLE["block_fill"])
        head(ws, row + 1, columns)
        return row + 2

    workbook = Workbook()

    # ---------------- 明细 ----------------
    ws = workbook.active
    ws.title = "明细"
    columns = [
        "序号", "交易日期", "交易时间", "金额(元)", "业务摘要", "交易场所", "交易后余额",
        "交易卡号", "交易账户", "交易户名", "源文件",
    ]
    ws["A1"] = f"收支详情明细（{len(report.entries)} 笔，支出合计 {report.total:,.2f} 元，{report.period}）"
    ws["A1"].font = Font(bold=True, size=11, color=_STYLE["block_font"])
    head(ws, 2, columns)
    for index, entry in enumerate(report.entries, 1):
        row = index + 2
        values = [
            index, entry.date, entry.time, entry.amount, entry.kind, entry.place,
            entry.balance, entry.fields.get("交易卡号", ""), entry.fields.get("交易账户", ""),
            entry.fields.get("交易户名", ""), entry.source,
        ]
        hit = round(entry.abs_amount, 2) in hits
        for column, value in enumerate(values, 1):
            cell = ws.cell(row=row, column=column, value=value)
            cell.border = border
            if column in (4, 7) and isinstance(value, (int, float)):
                cell.number_format = money
                cell.alignment = align["right"]
            elif column in (1, 2, 3, 5, 6, 8, 9, 10, 11):
                cell.alignment = align["center"]
            if hit:
                cell.fill = PatternFill("solid", fgColor=_STYLE["hit_fill"])
                if column in (4, 5):
                    cell.font = Font(bold=True, color=_STYLE["hit_font"])
    row = len(report.entries) + 4
    ws.cell(row=row, column=2, value="合计").font = Font(bold=True)
    cell = ws.cell(row=row, column=4, value=report.total)
    cell.number_format, cell.font = money, Font(bold=True)
    for index, width in enumerate([6, 12, 10, 12, 12, 24, 14, 14, 14, 10, 22], 1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A3"

    # ---------------- 汇总 ----------------
    summary = workbook.create_sheet("汇总")
    summary["A1"] = "收支详情汇总"
    summary["A1"].font = Font(bold=True, size=13, color=_STYLE["block_font"])
    metrics = [
        ("图片文件数", report.scanned_files),
        ("唯一截图数", len(report.entries)),
        ("重复文件数", report.duplicates),
        ("交易笔数", len(report.entries)),
        ("金额合计(元)", report.total),
    ]
    row = block(summary, 3, "概览", ["指标", "数值"])
    for label, value in metrics:
        for column, item in enumerate([label, value], 1):
            cell = summary.cell(row=row, column=column, value=item)
            cell.border = border
            cell.alignment = align["left"] if column == 1 else align["center"]
            if column == 2:
                cell.font = Font(bold=True, color=_STYLE["block_font"])
                if label == "金额合计(元)":
                    cell.number_format = money
        row += 1
    row += 1
    for title, rows, first in (
        ("按年", report.by_year, "年份"),
        ("按月", report.by_month, "月份"),
        ("按金额档", report.by_amount, "金额(元)"),
        ("按业务摘要", report.by_kind, "业务摘要"),
        ("按交易场所", report.by_place, "交易场所"),
    ):
        row = block(summary, row, title, [first, "笔数", "金额(元)", "金额占比"])
        for name, count, money_value in rows:
            share = round(money_value / report.total * 100, 1) if report.total else 0
            for column, value in enumerate([name, count, money_value, f"{share}%"], 1):
                cell = summary.cell(row=row, column=column, value=value)
                cell.border = border
                if column == 1:
                    cell.alignment = align["left"]
                else:
                    cell.alignment = align["center"]
                if column == 3:
                    cell.number_format = money
            row += 1
        total_row = ["合计", sum(c for _, c, _ in rows), round(sum(m for _, _, m in rows), 2), "100%"]
        for column, value in enumerate(total_row, 1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border, cell.font = border, Font(bold=True, color=_STYLE["block_font"])
            cell.fill = PatternFill("solid", fgColor=_STYLE["block_fill"])
            if column == 3:
                cell.number_format = money
        row += 2

    row = block(summary, row, "校验结果", ["检查项", "结果", "说明"])
    checks = [
        ("交易金额 = |顶部金额|", sum(1 for e in report.entries if e.amount_ok), len(report.entries)),
        ("7 个字段齐全", sum(1 for e in report.entries if e.fields_ok), len(report.entries)),
        ("交易时间可解析", sum(1 for e in report.entries if e.date and e.time), len(report.entries)),
    ]
    for name, passed, total_count in checks:
        values = [name, "通过" if passed == total_count else "有异常", f"{passed}/{total_count}"]
        for column, value in enumerate(values, 1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border = border
            cell.alignment = align["left"] if column != 2 else align["center"]
        fill = _STYLE["ok_fill"] if passed == total_count else _STYLE["bad_fill"]
        font = _STYLE["ok_font"] if passed == total_count else _STYLE["bad_font"]
        cell = summary.cell(row=row, column=2)
        cell.fill, cell.font = PatternFill("solid", fgColor=fill), Font(bold=True, color=font)
        row += 1
    for label, values in report.distinct.items():
        text = "唯一值" if len(values) == 1 else f"{len(values)} 种取值：{'、'.join(sorted(values))}"
        for column, value in enumerate([f"{label}一致性", "通过" if len(values) == 1 else "需确认", text], 1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border = border
            cell.alignment = align["left"] if column != 2 else align["center"]
        if len(values) > 1:
            cell = summary.cell(row=row, column=2)
            cell.fill = PatternFill("solid", fgColor=_STYLE["bad_fill"])
            cell.font = Font(bold=True, color=_STYLE["bad_font"])
        row += 1
    for entry in report.issues:
        for column, value in enumerate([entry.source, "异常", "；".join(entry.issues)], 1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border = border
            cell.alignment = align["left"]
        row += 1
    row += 1

    row = block(summary, row, "文件对照（重复文件 → 唯一截图）", ["唯一截图", "重复副本"])
    for entry in report.entries:
        for column, value in enumerate([entry.source, "、".join(entry.copies) or "—"], 1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border = border
            cell.alignment = align["left"]
        row += 1

    for index, width in enumerate([18, 26, 14, 12, 12, 14, 16], 1):
        summary.column_dimensions[get_column_letter(index)].width = width

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.save(path)
    except PermissionError as exc:
        raise PermissionError(f"无法写入 {path.name}，请先关闭同名文件") from exc
    return path


def selftest() -> None:
    """纯函数自检：验证字段配对、金额校验、时间拆分与汇总。"""

    class _T:
        def __init__(self, y, x, x2, text, height=40):
            self.y, self.x, self.x2, self.t, self.h = y, x, x2, text, height

    items = [
        _T(190, 420, 640, "-69.00", height=70),
        _T(240, 380, 700, "余额 17,447.64", height=36),
        _T(700, 40, 200, "交易卡号"), _T(700, 700, 950, "6222****1489"),
        _T(760, 40, 200, "交易账户"), _T(760, 700, 950, "3100****1510"),
        _T(820, 40, 200, "交易户名"), _T(820, 700, 860, "于翔"),
        _T(880, 40, 200, "交易时间"), _T(880, 700, 990, "2026-02-0112:10:31"),
        _T(940, 40, 200, "业务摘要"), _T(940, 700, 860, "消费"),
        _T(1000, 40, 200, "交易场所"), _T(1000, 700, 960, "宝付-好分期"),
        _T(1060, 40, 200, "交易金额"), _T(1060, 700, 860, "69.00"),
    ]
    entry = parse_entry(items, "a.png")
    assert entry.issues == [], entry.issues
    assert entry.amount == -69.0 and entry.balance == 17447.64
    assert entry.date == "2026-02-01" and entry.time == "12:10:31"
    assert entry.kind == "消费" and entry.place == "宝付-好分期"
    assert entry.fields["交易卡号"] == "6222****1489"

    # 金额不一致 + 缺字段必须被抓出来
    broken = list(items)
    broken[-1] = _T(1060, 700, 860, "199.00")
    bad = parse_entry([it for it in broken if not _norm(it.t).startswith("业务摘要")], "b.png")
    assert any("不一致" in issue for issue in bad.issues), bad.issues
    assert "缺字段：业务摘要" in bad.issues, bad.issues

    report = build_report([entry], scanned_files=2, duplicates=1)
    assert report.total == 69.0 and report.by_year == [("2026", 1, 69.0)]
    assert report.by_amount == [("69", 1, 69.0)]
    assert report.distinct["交易户名"] == {"于翔"}
    print("detail.selftest OK")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="收支详情单页 → 明细 + 多维汇总")
    parser.add_argument("folder", nargs="?", help="截图文件夹")
    parser.add_argument("--highlight", default="59,69,179,199", help="需要高亮的金额，逗号分隔")
    parser.add_argument("--out", help="输出 xlsx 路径")
    parser.add_argument("--selftest", action="store_true", help="跑纯函数自检")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0
    if not args.folder:
        parser.print_help()
        return 1
    highlight = {
        round(float(piece), 2)
        for piece in re.split(r"[,，、\s]+", args.highlight.strip())
        if piece
    }
    run(Path(args.folder).expanduser(), highlight, out=Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
