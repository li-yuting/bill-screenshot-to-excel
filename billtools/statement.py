"""好分期账单长截图：把 OCR 文字块解析成结构化记录，并做多段合并与勾稽校验。

本模块只依赖标准库（文字块用结构化 Protocol 描述），可以直接单测。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, Sequence

# 含「至」的文本是顶部汇总卡的账单区间（2025.01.01至2025.12.31），不能当日期分组头
DATE_RE = re.compile(r"^(\d{4})[-.](\d{1,2})[-.](\d{1,2})")
PERIOD_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*至\s*(\d{4})\.(\d{1,2})\.(\d{1,2})")
AMOUNT_RE = re.compile(r"[￥¥]\s*(-?)\s*([\d,]+)\.(\d{2})")
BALANCE_RE = re.compile(r"余额[:：]?\s*[￥¥]\s*(-?)\s*([\d,]+)\.(\d{2})")
EXPENSE_RE = re.compile(r"支出\s*[￥¥]?\s*([\d,]+\.\d{2})")
INCOME_RE = re.compile(r"收入\s*[￥¥]?\s*([\d,]+\.\d{2})")
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
CARD_RE = re.compile(r"^([^\d]+?)\d+$")

MIN_AMOUNT_H = 30
"""金额文字块的最低高度，低于此值说明该行被裁掉了。"""
TYPE_MIN_H, TYPE_MAX_H = 40, 75
"""「交易类型」大字的高度区间，用于跟同行的日期、小字区分。"""
AMOUNT_X_MIN = 600
"""金额右对齐，锁定右半屏。"""
LABEL_X_MAX = 400
"""交易类型在左半屏。"""
SUBLINE_GAP = 170
"""卡号/余额行位于金额下方的像素距离。"""
DATE_MATCH_GAP = 5


class TextBlock(Protocol):
    """OCR 文字块需要提供的最小结构（见 ocr.Item）。"""

    y: float
    y2: float
    x: float
    x2: float
    t: str

    @property
    def h(self) -> float: ...


@dataclass
class Summary:
    """页面顶部汇总卡。"""

    start: str
    end: str
    expense: float | None = None
    income: float | None = None

    @property
    def period(self) -> str:
        return f"{self.start}~{self.end}"

    @property
    def year(self) -> str:
        return self.start[:4]

    @property
    def label(self) -> str:
        return f"{self.start} 至 {self.end}"


@dataclass
class Record:
    """一笔交易。"""

    date: str
    kind: str
    amount: float
    card: str
    time: str
    balance: float | None
    y: float
    raw: str
    source: str = ""


@dataclass
class Page:
    """一张截图解析出的内容。"""

    source: str
    copies: list[str] = field(default_factory=list)
    summary: Summary | None = None
    records: list[Record] = field(default_factory=list)
    name: str = ""


@dataclass
class Group:
    """同一账单期间的若干段截图（合并去重后）。"""

    key: str
    pages: list[Page]
    records: list[Record]
    duplicates: int
    expense: float
    income: float
    card_expense: float | None
    card_income: float | None

    @property
    def year(self) -> str:
        summary = next((p.summary for p in self.pages if p.summary), None)
        if summary:
            return summary.year
        return self.records[0].date[:4] if self.records and self.records[0].date else "未知"

    @property
    def start(self) -> str:
        """账单区间起始日，用于把各期间按时间排序。"""
        summary = next((p.summary for p in self.pages if p.summary), None)
        if summary:
            return summary.start
        return self.records[0].date if self.records and self.records[0].date else "9999-99-99"

    @property
    def name(self) -> str:
        return self.pages[0].name if self.pages else self.year

    @property
    def period_label(self) -> str:
        summary = next((p.summary for p in self.pages if p.summary), None)
        return summary.label if summary else self.key

    @property
    def expense_diff(self) -> float | None:
        """支出合计与页面汇总卡的差额；无汇总卡时为 None。"""
        if self.card_expense is None:
            return None
        return round(self.expense - self.card_expense, 2)

    @property
    def income_diff(self) -> float | None:
        if self.card_income is None:
            return None
        return round(self.income - self.card_income, 2)


@dataclass
class _Amount:
    """候选金额框（合并重叠后）。"""

    y: float
    y2: float
    x: float
    value: float
    text: str

    @property
    def h(self) -> float:
        return self.y2 - self.y


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _split_number(sign: str, digits: str, cents: str) -> float:
    value = float(f"{digits.replace(',', '')}.{cents}")
    return -value if sign == "-" else value


def _to_money(text: str) -> float:
    return float(text.replace(",", ""))


def _ymd(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _clean_card(text: str) -> str:
    """`龙卡通088813:43` -> `龙卡通 0888`。"""
    flat = _norm(text)
    flat = re.sub(r"\d{1,2}:\d{2}$", "", flat)
    match = CARD_RE.match(flat)
    return f"{match.group(1)} {flat[len(match.group(1)):]}" if match else (flat or "—")


def parse_summary(items: Sequence[TextBlock]) -> Summary | None:
    """从页面顶部提取账单区间与支出/收入合计。

    Args:
        items: 该页的全部 OCR 文字块。

    Returns:
        解析出的 Summary；找不到账单区间时返回 None。
    """
    start = end = ""
    expense: float | None = None
    income: float | None = None
    for item in items:
        text = _norm(item.t)
        if not start:
            match = PERIOD_RE.search(text)
            if match:
                start = _ymd(match.group(1), match.group(2), match.group(3))
                end = _ymd(match.group(4), match.group(5), match.group(6))
        if expense is None:
            match = EXPENSE_RE.search(text)
            if match:
                expense = _to_money(match.group(1))
        if income is None:
            match = INCOME_RE.search(text)
            if match:
                income = _to_money(match.group(1))
    if not start:
        return None
    return Summary(start=start, end=end or start, expense=expense, income=income)


def _merge_overlapping(amounts: list[_Amount]) -> list[_Amount]:
    """合并重叠的金额框。

    同一个金额常被检测出大小两个框（例如 -123.67 与 -125.07），保留较高的那个：
    矮框往往是裁切/误识别产物，高框才是完整文本。
    """
    amounts.sort(key=lambda a: (a.y, -a.h))
    kept: list[_Amount] = []
    for cand in amounts:
        for exist in kept:
            overlap = min(cand.y2, exist.y2) - max(cand.y, exist.y)
            if overlap > 0.4 * min(cand.h, exist.h):
                if cand.h > exist.h:
                    exist.y, exist.y2, exist.x, exist.value, exist.text = (
                        cand.y,
                        cand.y2,
                        cand.x,
                        cand.value,
                        cand.text,
                    )
                break
        else:
            kept.append(cand)
    return kept


def parse_records(items: Sequence[TextBlock]) -> list[Record]:
    """把文字块解析成交易记录（按图片从上到下的顺序）。

    Args:
        items: 该页的全部 OCR 文字块。

    Returns:
        交易记录列表；日期无法归属时 ``date`` 为空字符串，稍后由同期间其它段回填。
    """
    headers: list[tuple[float, str]] = []
    for item in items:
        text = _norm(item.t)
        if "至" in text:
            continue
        match = DATE_RE.match(text)
        if match:
            headers.append((item.y, _ymd(match.group(1), match.group(2), match.group(3))))
    headers.sort()

    candidates: list[_Amount] = []
    for item in items:
        text = _norm(item.t)
        if "余额" in text or "支出" in text or "收入" in text:
            continue
        match = AMOUNT_RE.search(text)
        if match and item.x > AMOUNT_X_MIN and item.h >= MIN_AMOUNT_H:
            candidates.append(
                _Amount(
                    y=item.y,
                    y2=item.y2,
                    x=item.x,
                    value=_split_number(*match.groups()),
                    text=text,
                )
            )

    records: list[Record] = []
    for amount in sorted(_merge_overlapping(candidates), key=lambda a: a.y):
        date = ""
        for header_y, header_date in headers:
            if header_y < amount.y - DATE_MATCH_GAP:
                date = header_date

        kind = ""
        card = ""
        time_text = ""
        balance: float | None = None
        for item in items:
            text = _norm(item.t)
            if (
                not kind
                and item.x < LABEL_X_MAX
                and TYPE_MIN_H <= item.h <= TYPE_MAX_H
                and abs(item.y - amount.y) <= 40
                and not AMOUNT_RE.search(text)
                and not DATE_RE.match(text)
            ):
                kind = text
            if amount.y2 - 5 <= item.y <= amount.y2 + SUBLINE_GAP:
                match = BALANCE_RE.search(text)
                if match and item.x > AMOUNT_X_MIN:
                    balance = _split_number(*match.groups())
                elif not card and item.x < AMOUNT_X_MIN:
                    card = _clean_card(text)
                    found = TIME_RE.search(text)
                    if found:
                        time_text = found.group(1)

        records.append(
            Record(
                date=date,
                kind=kind,
                amount=round(amount.value, 2),
                card=card,
                time=time_text,
                balance=balance,
                y=amount.y,
                raw=amount.text,
            )
        )
    return records


def parse_page(items: Sequence[TextBlock], source: str, copies: Sequence[str] = ()) -> Page:
    """解析一张截图。

    Args:
        items: 该页的全部 OCR 文字块。
        source: 代表文件名。
        copies: 内容相同的副本文件名。

    Returns:
        该页的解析结果。
    """
    return Page(
        source=source,
        copies=list(copies),
        summary=parse_summary(items),
        records=parse_records(items),
    )


def _first_known_date(page: Page) -> str:
    for record in page.records:
        if record.date:
            return record.date
    return "9999-99-99"


def _same_record(left: Record, right: Record) -> bool:
    """判断两条记录是否为同一笔交易。

    日期+类型+金额+时间一致时，余额也一致才算同一笔；但余额可能识别不到（None），
    那时只能按前四项判定——否则重叠段里同一行会被重复计入。
    """
    if (left.date, left.kind, round(left.amount, 2), left.time) != (
        right.date,
        right.kind,
        round(right.amount, 2),
        right.time,
    ):
        return False
    if left.balance is None or right.balance is None:
        return True
    return abs(left.balance - right.balance) < 0.005


def _fill_unknown_dates(pages: Sequence[Page]) -> int:
    """用同期其它段里「金额+时间+余额」相同的记录，回填本段缺失的日期。

    段首被吸顶汇总卡遮住的那一行，会失去日期归属。
    """
    exact: dict[tuple, str] = {}
    loose: dict[tuple, str] = {}
    for page in pages:
        for record in page.records:
            if not record.date:
                continue
            loose.setdefault((round(record.amount, 2), record.time), record.date)
            if record.balance is not None:
                exact.setdefault((round(record.amount, 2), record.time, record.balance), record.date)
    filled = 0
    for page in pages:
        for record in page.records:
            if record.date:
                continue
            record.date = exact.get(
                (round(record.amount, 2), record.time, record.balance)
            ) or loose.get((round(record.amount, 2), record.time), "")
            filled += bool(record.date)
    return filled


def build_groups(pages: Sequence[Page]) -> list[Group]:
    """按账单区间归组，合并同期间的多段截图并去重。

    Args:
        pages: 各张截图的解析结果。

    Returns:
        每组含去重后的明细、合计，以及页面汇总卡金额（用于勾稽）。
    """
    buckets: "OrderedDict[str, list[Page]]" = OrderedDict()
    for page in pages:
        key = page.summary.period if page.summary else "未知区间"
        buckets.setdefault(key, []).append(page)

    groups: list[Group] = []
    for key, group_pages in buckets.items():
        group_pages = sorted(group_pages, key=_first_known_date)
        year = next((p.summary.year for p in group_pages if p.summary), "未知")
        for position, page in enumerate(group_pages, 1):
            page.name = f"{year}年" if len(group_pages) == 1 else f"{year}年-第{position}段"
        _fill_unknown_dates(group_pages)

        seen: dict[tuple, list[Record]] = {}
        merged: list[Record] = []
        duplicates = 0
        for page in group_pages:
            for record in page.records:
                bucket_key = (record.date, record.kind, round(record.amount, 2), record.time)
                bucket = seen.setdefault(bucket_key, [])
                if any(_same_record(record, exist) for exist in bucket):
                    duplicates += 1
                    continue
                bucket.append(record)
                record.source = page.name
                merged.append(record)
        merged.sort(key=lambda r: (r.date, r.time))

        summary = next((p.summary for p in group_pages if p.summary), None)
        groups.append(
            Group(
                key=key,
                pages=list(group_pages),
                records=merged,
                duplicates=duplicates,
                expense=round(sum(-r.amount for r in merged if r.amount < 0), 2),
                income=round(sum(r.amount for r in merged if r.amount > 0), 2),
                card_expense=summary.expense if summary else None,
                card_income=summary.income if summary else None,
            )
        )
    # 输出按账单区间的时间先后排序（截图文件名顺序不可靠）
    groups.sort(key=lambda g: g.start)
    return groups


def all_records(groups: Sequence[Group]) -> list[Record]:
    """把各组合并成一份全局去重明细（跨年不会有重复，直接拼接后排序）。"""
    merged = [record for group in groups for record in group.records]
    merged.sort(key=lambda r: (r.date, r.time))
    return merged


def selftest() -> None:
    """纯函数自检：不依赖 OCR，验证解析、去重、日期回填。"""

    class _T:
        def __init__(self, y, y2, x, x2, t):
            self.y, self.y2, self.x, self.x2, self.t = y, y2, x, x2, t

        @property
        def h(self):
            return self.y2 - self.y

    page_items = [
        _T(403, 446, 101, 762, "2025.01.01至2025.12.31"),
        _T(565, 614, 98, 434, "支出36,515.73"),
        _T(565, 614, 532, 739, "收入 20,083.00"),
        # 被吸顶汇总卡遮住日期头的段首行：日期必须留空，等同期其它段回填
        _T(939, 995, 67, 180, "代收付"),
        _T(943, 992, 808, 1070, "￥-131.48"),
        _T(1034, 1076, 711, 1074, "余额：￥14,831.06"),
        _T(1035, 1072, 74, 400, "龙卡通088814:23"),
        _T(1240, 1272, 80, 300, "2025-12-1周一"),
        _T(1350, 1405, 67, 180, "代收付"),
        _T(1353, 1402, 808, 1070, "￥ -7.09"),
        _T(1444, 1487, 803, 1074, "余额：￥12,284.68"),
        _T(1446, 1483, 74, 400, "龙卡通088802:08"),
        # 同一金额的大/小两个框：必须只保留较高的那个
        _T(2030, 2085, 67, 180, "消费"),
        _T(2040, 2093, 807, 1070, "￥ -123.67"),
        _T(2064, 2091, 813, 1064, "￥-125.07"),
        _T(2132, 2173, 800, 1074, "余额：￥38.09"),
        _T(2132, 2173, 74, 400, "龙卡通088802:08"),
    ]

    summary = parse_summary(page_items)
    assert summary is not None
    assert summary.year == "2025" and summary.expense == 36515.73 and summary.income == 20083.00

    records = parse_records(page_items)
    assert len(records) == 3, [r.amount for r in records]
    assert records[0].date == "", "段首行不应被汇总卡区间误当成日期"
    assert records[0].amount == -131.48 and records[0].kind == "代收付"
    assert records[0].balance == 14831.06 and records[0].card == "龙卡通 0888"
    assert records[0].time == "14:23"
    assert records[1].date == "2025-12-01" and records[1].time == "02:08"
    assert records[2].amount == -123.67, "重叠金额框应保留较高者"

    # 两段同一账单区间：日期回填 + 跨段去重 + 段落命名
    page_a = Page(source="a.png", summary=summary, records=parse_records(page_items))
    page_b = Page(
        source="b.png",
        summary=summary,
        records=[Record("2025-12-03", "代收付", -131.48, "龙卡通 0888", "14:23", 14831.06, 940.0, "")],
    )
    groups = build_groups([page_a, page_b])
    assert len(groups) == 1
    group = groups[0]
    assert page_a.name == "2025年-第1段" and page_b.name == "2025年-第2段"
    assert group.duplicates == 1, group.duplicates
    assert sorted({r.date for r in group.records}) == ["2025-12-01", "2025-12-03"]
    assert group.expense == 262.24, group.expense
    assert group.card_expense == 36515.73
    assert group.expense_diff == round(262.24 - 36515.73, 2)

    # 一侧余额没识别到（None）时，同一行仍必须去重
    page_c = Page(
        source="c.png",
        summary=summary,
        records=[Record("2025-12-03", "代收付", -131.48, "龙卡通 0888", "14:23", None, 0.0, "")],
    )
    groups = build_groups([page_a, page_c])
    assert groups[0].duplicates == 1, groups[0].duplicates
    assert groups[0].expense == 262.24, groups[0].expense

    print("statement.selftest OK")


if __name__ == "__main__":
    selftest()
