"""账单列表长截图：把 OCR 文字块解析成结构化记录，并做多段合并与勾稽校验。

定位方式是**自校准**，不使用任何绝对像素阈值。理由见
``docs/superpowers/specs/2026-09-18-bill-statement-selfcalib-design.md``：
手机分辨率、系统缩放、企业微信转发压缩、图片查看器显示比例都无法预设，
所以「金额块必须在 x>600」这类常量一旦换台手机就全废。

改成从每张图**自己**的金额块里量出尺度：

- ``row_pitch``  相邻金额块 y 间距的中位数 —— 每一笔占多高
- ``money_h``    金额块高度中位数 —— 金额字号多大
- ``money_x``    金额块左边界中位数 —— 金额列从哪儿开始

依据是两条与分辨率无关的性质：金额块的文本形状（正则），以及「右对齐 + 单行 + 等距」（版式）。

本模块只依赖标准库（文字块用结构化 Protocol 描述），可以直接单测。
"""

from __future__ import annotations

import re
import statistics
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, Sequence

PERIOD_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*至\s*(\d{4})\.(\d{1,2})\.(\d{1,2})")
DATE_RE = re.compile(r"^(\d{4})[-.](\d{1,2})[-.](\d{1,2})")
AMOUNT_RE = re.compile(r"[￥¥]\s*(-?)\s*([\d,]+)\.(\d{2})")
BALANCE_RE = re.compile(r"余额[:：]?\s*[￥¥]\s*(-?)\s*([\d,]+)\.(\d{2})")
EXPENSE_RE = re.compile(r"支出\s*[￥¥]?\s*([\d,]+\.\d{2})")
INCOME_RE = re.compile(r"收入\s*[￥¥]?\s*([\d,]+\.\d{2})")
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
CARD_TAIL_RE = re.compile(r"^([^\d]+?)(\d+)$")

CJK_PUNCT = {"，": ",", "。": ".", "、": ",", "：": ":"}
THOUSAND_FIX_RE = re.compile(r"(\d)\.(\d{3})\.(\d{2})")

SAME_LINE_RATIO = 0.45
"""``|块.y - 金额.y| <= 该比例 × row_pitch`` 视为同一行（而不是绝对像素）。"""
MARK_TOLERANCE_RATIO = 0.25
"""红色标注框的纵向容差，同样按 row_pitch 缩放。"""
MAX_MONEY_CHARS = 14
"""金额块自身的字符数上限。超了说明这是夹着金额的长文本块（如「渠道名+余额」），不是金额块。"""
MIN_ANCHORS = 2
"""少于这么多金额块，说明本图没有交易列表，直接判空而不是硬凑。"""
DEFAULT_PITCH = 100.0
"""锚点太少时 row_pitch 的兜底值，仅用于避免除零。"""
MONEY_EPS = 0.005
"""金额/余额比较的容差。"""


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
    """页面顶部汇总卡。区间可能读不到（被窗口裁掉），此时只填金额。"""

    start: str = ""
    end: str = ""
    expense: float | None = None
    income: float | None = None

    @property
    def has_period(self) -> bool:
        return bool(self.start)

    @property
    def period(self) -> str:
        return f"{self.start}~{self.end}" if self.has_period else "未知区间"

    @property
    def year(self) -> str:
        return self.start[:4] if self.has_period else "未知"

    @property
    def year_label(self) -> str:
        """区间跨年时写成年份范围，避免把 2023–2026 的查询叫成「2023年」。"""
        if not self.has_period:
            return "未知"
        head, tail = self.start[:4], self.end[:4]
        return f"{head}–{tail}年" if head != tail else f"{head}年"

    @property
    def label(self) -> str:
        return f"{self.start} 至 {self.end}" if self.has_period else "区间未知"


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
    flagged: bool = False
    """被用户红框圈住 —— 需要特别核对，输出时红色高亮。"""
    uncertain: bool = False
    """余额与渠道都没读到，去重无法判定，按「宁可重复也不丢失」保留。"""


@dataclass
class Page:
    """一张截图解析出的内容。"""

    source: str
    copies: list[str] = field(default_factory=list)
    summary: Summary | None = None
    records: list[Record] = field(default_factory=list)
    name: str = ""
    marks: int = 0
    """本图上的红色标注框个数。"""

    @property
    def tier(self) -> float | None:
        """主要金额档：本图出现次数最多的金额绝对值。"""
        return dominant_tier(self.records)


@dataclass(frozen=True)
class Scale:
    """从单张图里量出的版式尺度 —— 无量纲定位的全部依据。"""

    row_pitch: float
    money_h: float
    money_x: float
    anchors: int


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
    cross: list[tuple[str, str, int]] = field(default_factory=list)
    """跨图重复：[(保留的图, 被合并的图, 笔数)]。"""

    def _summary(self) -> Summary | None:
        return next((p.summary for p in self.pages if p and p.summary), None)

    @property
    def year(self) -> str:
        summary = self._summary()
        if summary and summary.has_period:
            return summary.year
        return self.year_label

    @property
    def year_label(self) -> str:
        summary = self._summary()
        if summary and summary.has_period:
            return summary.year_label
        tier = dominant_tier(self.records)
        return f"{tier:g}元档" if tier is not None else "未知"

    @property
    def start(self) -> str:
        """账单区间起始日，用于把各期间按时间排序。"""
        summary = self._summary()
        if summary and summary.has_period:
            return summary.start
        dated = sorted(r.date for r in self.records if r.date)
        return dated[0] if dated else "9999-99-99"

    @property
    def name(self) -> str:
        return self.pages[0].name if self.pages else self.year_label

    @property
    def period_label(self) -> str:
        summary = self._summary()
        if summary and summary.has_period:
            return summary.label
        dated = sorted(r.date for r in self.records if r.date)
        if dated:
            return f"{dated[0]} 至 {dated[-1]}（推自明细）"
        return "区间未知"

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

    @property
    def gap_note(self) -> str:
        """把差额翻译成人话：差额能被主要金额档整除时，说清是几笔。"""
        diff = self.expense_diff
        if diff is None or abs(diff) < MONEY_EPS:
            return ""
        tier = dominant_tier(self.records)
        if not tier:
            return f"差 {abs(diff):,.2f} 元。"
        count = abs(diff) / tier
        if abs(count - round(count)) > 0.001:
            return (
                f"差 {abs(diff):,.2f} 元，不是 {tier:g} 元的整数倍，无法归因到「整笔遗漏」，"
                f"请抽查明细核对。"
            )
        written = "明细比页面汇总卡多" if diff > 0 else "明细比页面汇总卡少，疑为长截图拼接处漏抓一行"
        return (
            f"差 {abs(diff):,.2f} = {round(count):d} 笔 {tier:g} 元。"
            f"{written}，建议补截该期间账单核对 —— 这是截图缺口，不是识别错误。"
        )


def _norm(text: str) -> str:
    """去空白 + 中文标点归一化（OCR 会把半角逗号读成全角逗号）。"""
    flat = re.sub(r"\s+", "", text)
    for bad, good in CJK_PUNCT.items():
        flat = flat.replace(bad, good)
    return flat


def _repair_thousand(text: str) -> str:
    """修 OCR 把千分位逗号读成句点的情况：``4.290.70`` -> ``4,290.70``。

    判据是「第一个分隔符后面正好 3 位数字」—— 那几乎不可能是小数。
    """
    return THOUSAND_FIX_RE.sub(r"\1,\2.\3", text)


def _split_number(sign: str, digits: str, cents: str) -> float:
    value = float(f"{digits.replace(',', '')}.{cents}")
    return -value if sign == "-" else value


def _to_money(text: str) -> float:
    return float(text.replace(",", ""))


def _ymd(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _flat(item: TextBlock) -> str:
    return _norm(item.t)


def _is_noise(text: str) -> bool:
    """是不是截图工具的界面文字（既无汉字也无字母）。

    单屏截图里常见「图片」窗口标题栏、底部工具栏的箭头、``[1:1]`` 缩放标记
    （还有单屏截图底部那个被 OCR 读成 ``[1:1]`` 的比例指示）。这些块没有汉字也没有
    英文字母，可以安全排除 —— 真实商户名不可能一个字都没有。
    """
    return not re.search(r"[\u4e00-\u9fffA-Za-z]", text)


def money_of(item: TextBlock) -> float | None:
    """把一个文字块当成「金额块」来取值；不是金额块则返回 None。

    排除含「余额 / 支出 / 收入」的块（那是余额或汇总卡），并限制字符数，
    避免把「渠道名 + 余额」这种被 OCR 合并的长文本块误当金额。
    """
    text = _flat(item)
    if "余额" in text or "支出" in text or "收入" in text:
        return None
    if len(text) > MAX_MONEY_CHARS:
        return None
    match = AMOUNT_RE.search(_repair_thousand(text))
    return _split_number(*match.groups()) if match else None


def calibrate(items: Sequence[TextBlock]) -> Scale | None:
    """从本图的金额块量出版式尺度。量不出来说明这张图里没有交易列表。

    Args:
        items: 该页的全部 OCR 文字块。

    Returns:
        量出的 Scale；金额块少于 ``MIN_ANCHORS`` 个时返回 None。
    """
    anchors = [it for it in items if money_of(it) is not None]
    if len(anchors) < MIN_ANCHORS:
        return None
    anchors = sorted(anchors, key=lambda it: it.y)
    diffs = [
        second.y - first.y
        for first, second in zip(anchors, anchors[1:])
        if second.y - first.y > 1
    ]
    return Scale(
        row_pitch=statistics.median(diffs) if diffs else DEFAULT_PITCH,
        money_h=statistics.median([it.h for it in anchors]),
        money_x=statistics.median([it.x for it in anchors]),
        anchors=len(anchors),
    )


class _Proxy:
    """文字块的浅拷贝，用于在合并重叠框时替换矮框。"""

    __slots__ = ("y", "y2", "x", "x2", "t", "h")

    def __init__(self, item: TextBlock):
        self.y, self.y2, self.x, self.x2 = item.y, item.y2, item.x, item.x2
        self.t = item.t
        self.h = item.y2 - item.y


def _merge_overlapping(anchors: list[TextBlock]) -> list[TextBlock]:
    """合并重叠的金额框。

    同一个金额常被检测出大小两个框（例如 -123.67 与 -125.07），保留较高的那个：
    矮框往往是裁切/误识别产物，高框才是完整文本。
    """
    ordered = sorted(anchors, key=lambda it: (it.y, -it.h))
    kept: list[TextBlock] = []
    for candidate in ordered:
        for index, exist in enumerate(kept):
            overlap = min(candidate.y2, exist.y2) - max(candidate.y, exist.y)
            if overlap > 0.4 * min(candidate.h, exist.h):
                if candidate.h > exist.h:
                    kept[index] = _Proxy(candidate)
                break
        else:
            kept.append(candidate)
    return kept


def _clean_card(text: str) -> str:
    """``龙卡通088814:23`` -> ``龙卡通 0888``；``财付通-…余额：￥1.00`` -> 只留渠道名。"""
    flat = _norm(text)
    if "余额" in flat:
        flat = flat[: flat.index("余额")].strip("-　 ")
    flat = re.sub(r"\d{1,2}:\d{2}$", "", flat)
    if not flat:
        return ""
    match = CARD_TAIL_RE.match(flat)
    return f"{match.group(1)} {match.group(2)}" if match else flat


def parse_summary(items: Sequence[TextBlock]) -> Summary | None:
    """从页面顶部提取账单区间与支出/收入合计。

    只要读到支出或收入就返回 Summary —— 区间可能被截图工具裁掉（例如单看某个
    金额档的列表图），此时区间留空，但金额仍要参与勾稽。

    Args:
        items: 该页的全部 OCR 文字块。

    Returns:
        解析出的 Summary；区间和金额都没读到则返回 None。
    """
    start = end = ""
    expense: float | None = None
    income: float | None = None
    for item in items:
        text = _flat(item)
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
    if not start and expense is None and income is None:
        return None
    return Summary(start=start, end=end or start, expense=expense, income=income)


def _marked(anchor_y: float, marks: Sequence[Sequence[float]], row_pitch: float) -> bool:
    """这笔是否落在用户画的红框里。容差按 row_pitch 缩放，不用绝对像素。"""
    tolerance = MARK_TOLERANCE_RATIO * row_pitch
    return any(
        top - tolerance <= anchor_y <= bottom + tolerance
        for _left, top, _right, bottom in marks
    )


def parse_records(
    items: Sequence[TextBlock],
    marks: Sequence[Sequence[float]] = (),
) -> list[Record]:
    """把文字块解析成交易记录（按图片从上到下的顺序）。

    Args:
        items: 该页的全部 OCR 文字块。
        marks: OCR 阶段检测到的红色标注框 ``(左, 上, 右, 下)``。

    Returns:
        交易记录列表；日期无法归属时 ``date`` 为空字符串，稍后由同期间其它段回填。
    """
    scale = calibrate(items)
    if scale is None:
        return []

    anchors = _merge_overlapping([it for it in items if money_of(it) is not None])
    anchors.sort(key=lambda it: it.y)
    same_line_gap = SAME_LINE_RATIO * scale.row_pitch

    headers: list[tuple[float, str]] = []
    for item in items:
        text = _flat(item)
        if "至" in text:
            continue
        match = DATE_RE.match(text)
        if match:
            headers.append((item.y, _ymd(match.group(1), match.group(2), match.group(3))))
    headers.sort()

    records: list[Record] = []
    for index, anchor in enumerate(anchors):
        # 用「下一笔金额的上沿」当上界，就不需要「下方 170px」这类绝对常量
        next_y = anchors[index + 1].y if index + 1 < len(anchors) else float("inf")

        # 摘要（交易类型）：与金额同一行、且在金额左侧的块，取纵向最接近的那个
        same_line = [
            it for it in items
            if it is not anchor
            and money_of(it) is None
            and "余额" not in _flat(it)
            and not DATE_RE.match(_flat(it))
            and abs(it.y - anchor.y) <= same_line_gap
            and it.x2 <= anchor.x
        ]
        kind = min(same_line, key=lambda it: abs(it.y - anchor.y)).t if same_line else ""

        # 渠道 / 卡号 / 时间 / 余额：纵向落在 [本笔金额下沿, 下一笔金额上沿)
        balance: float | None = None
        card = ""
        time_text = ""
        for item in items:
            if not (anchor.y2 - 0.3 * anchor.h <= item.y < next_y):
                continue
            if item is anchor or money_of(item) is not None:
                continue
            if DATE_RE.match(_flat(item)):
                continue
            text = _flat(item)
            match = BALANCE_RE.search(_repair_thousand(text))
            if match:
                balance = _split_number(*match.groups())
                head = _clean_card(text)
                if head and not card:
                    card = head
                continue
            cleaned = _clean_card(text)
            # 渠道和「摘要」一样在金额列左侧。这条门禁同时挡掉了截图工具界面里那些
            # 贴着金额列的文字（例如查看器的 ``[1:1]`` 缩放标记），
            # 否则它会被当成渠道名，进而把本该相同的两笔判成不同、去重失效。
            if cleaned and not card and not _is_noise(text) and item.x2 <= anchor.x:
                card = cleaned
                found = TIME_RE.search(text)
                if found:
                    time_text = found.group(1)

        date = ""
        for header_y, header_date in headers:
            if header_y < anchor.y:
                date = header_date

        records.append(
            Record(
                date=date,
                kind=_norm(kind),
                amount=round(money_of(anchor) or 0.0, 2),
                card=card,
                time=time_text,
                balance=balance,
                y=anchor.y,
                raw=_norm(anchor.t),
                flagged=_marked(anchor.y, marks, scale.row_pitch),
                uncertain=balance is None and not card,
            )
        )
    return records


def parse_page(
    items: Sequence[TextBlock],
    source: str,
    copies: Sequence[str] = (),
    marks: Sequence[Sequence[float]] = (),
) -> Page:
    """解析一张截图。

    Args:
        items: 该页的全部 OCR 文字块。
        source: 代表文件名。
        copies: 内容相同的副本文件名。
        marks: 该图上的红色标注框。

    Returns:
        该页的解析结果。
    """
    return Page(
        source=source,
        copies=list(copies),
        summary=parse_summary(items),
        records=parse_records(items, marks),
        marks=len(marks),
    )


def dominant_tier(records: Sequence[Record]) -> float | None:
    """主要金额档：出现次数最多的金额绝对值。无汇总卡时用它兜底归组与解释缺口。"""
    if not records:
        return None
    counts: dict[float, int] = {}
    for record in records:
        value = round(abs(record.amount), 2)
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _first_known_date(page: Page) -> str:
    for record in page.records:
        if record.date:
            return record.date
    return "9999-99-99"


def _same_record(left: Record, right: Record) -> bool:
    """判断两条记录是否为同一笔交易。

    日期 + 类型 + 金额 + 时间一致时，再往下比余额；余额比不了就比渠道；
    **只有全都比不了时才认作同一笔** —— 否则重叠段里同一行会被重复计入。

    建行版式行内没有「时间」字段，金额又高度重复（大量 -59.00），
    所以必须靠余额/渠道兜住，不能只看前四项。
    """
    if (left.date, left.kind, round(left.amount, 2), left.time) != (
        right.date,
        right.kind,
        round(right.amount, 2),
        right.time,
    ):
        return False
    if left.balance is not None and right.balance is not None:
        return abs(left.balance - right.balance) < MONEY_EPS
    if left.card and right.card:
        return left.card == right.card
    return True


def _fill_unknown_dates(pages: Sequence[Page]) -> int:
    """用同期其它段里「金额+时间+余额」相同的记录，回填本段缺失的日期。

    段首被吸顶汇总卡遮住的那一行会失去日期归属。**必须在去重之前做** ——
    否则同一行在两段里一个有日期、一个没有，去重键就对不上，会被重复计入。
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


def _dedupe(all_pages: Sequence[Page]) -> list[tuple[str, str, int]]:
    """全局语义去重（跨图）。返回 ``[(保留的图, 被合并的图, 笔数)]``。

    必须跨图做：同一笔交易可能既在长截图里、又在「图片查看器」窗口截图里，
    而这两张图若分属不同组（有无汇总卡），组内去重是拦不住它的。
    """
    seen: dict[tuple, list[Record]] = {}
    origin: dict[int, str] = {}
    cross: dict[tuple[str, str], int] = {}
    for page in all_pages:
        kept: list[Record] = []
        for record in page.records:
            bucket_key = (record.date, record.kind, round(record.amount, 2), record.time)
            bucket = seen.setdefault(bucket_key, [])
            twin = next(
                (
                    exist for exist in bucket
                    if _same_record(record, exist)
                    # 两边都「读不到余额也读不到渠道」时不给合并 ——
                    # 无法判定，宁可重复也不静默丢一笔
                    and not (record.uncertain and exist.uncertain)
                ),
                None,
            )
            if twin is not None:
                pair = (origin[id(twin)], page.source)
                cross[pair] = cross.get(pair, 0) + 1
                continue
            bucket.append(record)
            origin[id(record)] = page.source
            kept.append(record)
        page.records = kept
    return [(kept, dropped, count) for (kept, dropped), count in cross.items()]


def build_groups(pages: Sequence[Page]) -> list[Group]:
    """先归组、回填日期，再全局去重，最后汇总。

    归组规则：

    1. 有汇总卡的图各自成组（锚点组）；
    2. 无汇总卡的图取其「主要金额档」，并入主要金额档相同的锚点组；
       匹配不上（或有多个候选）则单列一组。

    Args:
        pages: 各张截图的解析结果。

    Returns:
        每组含去重后的明细、合计，以及页面汇总卡金额（用于勾稽）。
    """
    all_pages = list(pages)

    buckets: "OrderedDict[str, list[Page]]" = OrderedDict()
    orphans: list[Page] = []
    for page in all_pages:
        if page.summary is not None:
            buckets.setdefault(page.summary.period, []).append(page)
        else:
            orphans.append(page)

    anchor_tiers = {
        key: dominant_tier([r for p in group_pages for r in p.records])
        for key, group_pages in buckets.items()
    }
    unmatched: list[Page] = []
    for page in orphans:
        tier = page.tier
        candidates = [k for k, v in anchor_tiers.items() if tier is not None and v == tier]
        if len(candidates) == 1:
            buckets[candidates[0]].append(page)
        else:
            unmatched.append(page)
    if unmatched:
        buckets["未知区间"] = unmatched

    for group_pages in buckets.values():
        _fill_unknown_dates(group_pages)
    cross = _dedupe(all_pages)

    groups: list[Group] = []
    for key, group_pages in buckets.items():
        ordered = sorted(group_pages, key=_first_known_date)
        summary = next((p.summary for p in ordered if p.summary), None)
        tier = dominant_tier([r for p in ordered for r in p.records])
        if summary is not None and summary.has_period:
            label = summary.year_label
        else:
            # 汇总卡在、但区间被裁掉（单看某个金额档的列表图）：用金额档当标签
            label = f"{tier:g}元档" if tier is not None else "未知"
        for position, page in enumerate(ordered, 1):
            page.name = label if len(ordered) == 1 else f"{label}-第{position}段"

        merged: list[Record] = []
        for page in ordered:
            for record in page.records:
                record.source = page.name
                merged.append(record)
        merged.sort(key=lambda r: (r.date or "9999", r.time))

        my_cross = [
            (kept, dropped, count) for kept, dropped, count in cross
            if any(p.source == dropped for p in ordered)
        ]
        groups.append(
            Group(
                key=key,
                pages=ordered,
                records=merged,
                duplicates=sum(count for _kept, _dropped, count in my_cross),
                expense=round(sum(-r.amount for r in merged if r.amount < 0), 2),
                income=round(sum(r.amount for r in merged if r.amount > 0), 2),
                card_expense=summary.expense if summary else None,
                card_income=summary.income if summary else None,
                cross=my_cross,
            )
        )
    # 输出按账单区间的时间先后排序（截图文件名顺序不可靠）
    groups.sort(key=lambda g: g.start)
    return groups


def all_records(groups: Sequence[Group]) -> list[Record]:
    """把各组合并成一份全局去重明细（已跨组去重，直接拼接后排序）。"""
    merged = [record for group in groups for record in group.records]
    merged.sort(key=lambda r: (r.date or "9999", r.time))
    return merged


def selftest() -> None:
    """纯函数自检：不依赖 OCR，验证自校准、解析、红框标记、去重、日期回填。"""

    class _T:
        def __init__(self, y, y2, x, x2, t):
            self.y, self.y2, self.x, self.x2, self.t = y, y2, x, x2, t

        @property
        def h(self):
            return self.y2 - self.y

    def haofenqi_items(offset: float = 0.0) -> list:
        """好分期版式的三行样例。"""
        return [
            _T(403 + offset, 446 + offset, 101, 762, "2025.01.01至2025.12.31"),
            _T(565 + offset, 614 + offset, 98, 434, "支出36,515.73"),
            _T(565 + offset, 614 + offset, 532, 739, "收入 20,083.00"),
            _T(939 + offset, 995 + offset, 67, 180, "代收付"),
            _T(943 + offset, 992 + offset, 808, 1070, "￥-131.48"),
            _T(1034 + offset, 1076 + offset, 711, 1074, "余额：￥14,831.06"),
            _T(1035 + offset, 1072 + offset, 74, 400, "龙卡通088814:23"),
            _T(1240 + offset, 1272 + offset, 80, 300, "2025-12-1周一"),
            _T(1350 + offset, 1405 + offset, 67, 180, "代收付"),
            _T(1353 + offset, 1402 + offset, 808, 1070, "￥ -7.09"),
            _T(1444 + offset, 1487 + offset, 803, 1074, "余额：￥12,284.68"),
            _T(1446 + offset, 1483 + offset, 74, 400, "龙卡通088802:08"),
            # 同一金额的大/小两个框：必须只保留较高的那个
            _T(2030 + offset, 2085 + offset, 67, 180, "消费"),
            _T(2040 + offset, 2093 + offset, 807, 1070, "￥ -123.67"),
            _T(2064 + offset, 2091 + offset, 813, 1064, "￥-125.07"),
            _T(2132 + offset, 2173 + offset, 800, 1074, "余额：￥38.09"),
            _T(2132 + offset, 2173 + offset, 74, 400, "龙卡通088802:08"),
        ]

    items = haofenqi_items()
    summary = parse_summary(items)
    assert summary is not None
    assert summary.year == "2025" and summary.expense == 36515.73 and summary.income == 20083.00
    assert summary.year_label == "2025年" and summary.label == "2025-01-01 至 2025-12-31"

    scale = calibrate(items)
    assert scale is not None and scale.anchors == 4, scale
    assert scale.money_x > 700, "金额列在右半屏 —— 但这是量出来的，不是写死的"

    records = parse_records(items)
    assert len(records) == 3, [r.amount for r in records]
    assert records[0].date == "", "段首行不应被汇总卡区间误当成日期"
    assert records[0].amount == -131.48 and records[0].kind == "代收付"
    assert records[0].balance == 14831.06 and records[0].card == "龙卡通 0888"
    assert records[0].time == "14:23"
    assert records[1].date == "2025-12-01" and records[1].time == "02:08"
    assert records[2].amount == -123.67, "重叠金额框应保留较高者"
    assert not any(r.flagged for r in records), "没有红框时不该有记录被标记"

    # 红框：容差按 row_pitch 缩放，圈住第一笔就只该标记第一笔
    marked = parse_records(items, marks=[(60.0, 930.0, 1080.0, 1000.0)])
    assert [r.flagged for r in marked] == [True, False, False], [r.flagged for r in marked]

    # 两段同一账单区间：日期回填 + 跨段去重 + 段落命名
    page_a = parse_page(haofenqi_items(), "a.png")
    page_b = parse_page(haofenqi_items(), "b.png")
    page_b.records = [
        Record("2025-12-03", "代收付", -131.48, "龙卡通 0888", "14:23", 14831.06, 940.0, "")
    ]
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
    page_c = parse_page(haofenqi_items(), "c.png")
    page_c.records = [
        Record("2025-12-03", "代收付", -131.48, "龙卡通 0888", "14:23", None, 0.0, "")
    ]
    groups = build_groups([page_a, page_c])
    assert groups[0].duplicates == 1, groups[0].duplicates
    assert groups[0].expense == 262.24, groups[0].expense

    # ---- 建行版式：行内没有「时间」，金额高度重复 ----
    def ccb_items() -> list:
        rows = [_T(100, 120, 40, 160, "2025-9-12周五")]
        for index, (y, balance) in enumerate(((140, "5,613.60"), (250, "6,344.38"))):
            rows.append(_T(y, y + 34, 32, 95, "消费"))
            rows.append(_T(y + 2, y + 36, 433, 563, "￥-59.00"))
            rows.append(_T(y + 48, y + 74, 36, 261, f"宝付支付-好分期{index}"))
            rows.append(_T(y + 48, y + 76, 383, 561, f"余额：￥{balance}"))
        rows.append(_T(360, 380, 40, 160, "2025-9-1周一"))
        rows.append(_T(400, 434, 32, 95, "消费"))
        rows.append(_T(402, 436, 433, 563, "￥-59.00"))
        return rows

    ccb = parse_records(ccb_items())
    assert len(ccb) == 3, [r.amount for r in ccb]
    assert ccb[0].balance == 5613.60 and ccb[1].balance == 6344.38
    assert ccb[0].date == "2025-09-12" and ccb[1].date == "2025-09-12"
    assert ccb[2].date == "2025-09-01"
    assert ccb[2].uncertain, "余额与渠道都缺失时该标记为去重不确信"

    # 同一天两笔同额、余额不同：绝不能被当成同一笔
    merged = build_groups([Page(source="ccb.png", summary=None, records=ccb)])[0]
    assert len(merged.records) == 3, [r.balance for r in merged.records]

    # 截图工具界面文字贴着金额列（查看器的 [1:1] 缩放标记）不许被当成渠道名 ——
    # 一旦被当成渠道，同一笔在两张图里就会因渠道不同而被判成两笔，跨图去重失效
    viewer = parse_records([
        _T(200, 234, 32, 95, "消费退货"),
        _T(200, 234, 435, 563, "￥59.00"),
        _T(240, 258, 36, 261, "宝付支付-好分期"),
        _T(240, 260, 383, 561, "余额：￥8,510.91"),
        _T(300, 314, 197, 249, "消费退货"),
        _T(300, 314, 373, 426, "￥59.00"),
        _T(352, 370, 358, 376, "[1:1]"),
    ])
    assert len(viewer) == 2, [r.amount for r in viewer]
    assert viewer[0].card == "宝付支付-好分期" and viewer[0].balance == 8510.91
    assert viewer[1].card == "", f"[1:1] 不许被当成渠道：{viewer[1].card!r}"
    assert viewer[1].uncertain, "余额和渠道都没读到时应标记去重不确信"
    assert _is_noise("[1:1]") and _is_noise("164%") and _is_noise("+")
    assert not _is_noise("宝付支付-好分期") and not _is_noise("STARBUCKS")

    # 上面那笔 +59 在两张图里各出现一次：必须被跨图去重
    group = build_groups([
        Page(source="long.png", summary=None, records=viewer[:1]),
        Page(source="view.png", summary=None, records=[viewer[1]] + ccb[:1]),
    ])[0]
    assert group.duplicates == 1, group.duplicates

    # 同一笔在不同图里各记一次（长截图 + 窗口截图）：跨图必须去重
    twin_a = Page(source="long.png", summary=None, records=[
        Record("2025-10-19", "消费退货", 59.0, "", "", 8510.91, 0, ""),
        Record("2025-10-12", "消费", -59.0, "宝付", "", 8432.01, 10, ""),
    ])
    twin_b = Page(source="window.png", summary=None, records=[
        Record("2025-10-19", "消费退货", 59.0, "", "", 8510.91, 0, ""),
        Record("2026-04-10", "消费", -59.0, "宝付", "", 9465.80, 10, ""),
    ])
    group = build_groups([twin_a, twin_b])[0]
    assert len(group.records) == 3, len(group.records)
    assert group.duplicates == 1 and group.cross[0][1] == "window.png", group.cross

    # 文本归一化：中文标点 + 千分位被读成句点
    assert _repair_thousand("余额：￥4.290.70") == "余额：￥4,290.70"
    assert _norm("余额：￥8，510.91") == "余额:￥8,510.91"
    assert _norm("余额：￥8，510.91") == _norm("余额:￥8,510.91")

    # 无汇总卡的图按主要金额档并入锚点组；跨年区间写成范围
    anchor = Page(
        source="long.png",
        summary=Summary("2023-01-01", "2026-09-18", 118.00, 59.00),
        records=[
            Record("2025-10-19", "消费退货", 59.0, "", "", 8510.91, 0, ""),
            Record("2025-10-12", "消费", -59.0, "宝付", "", 8432.01, 10, ""),
        ],
    )
    orphan = Page(source="window.png", summary=None, records=[
        Record("2026-04-10", "消费", -59.0, "宝付", "", 9465.80, 0, ""),
        Record("2026-03-11", "消费", -59.0, "宝付", "", 5150.51, 10, ""),
    ])
    other = Page(
        source="window2.png",
        summary=Summary("", "", 774.00, 258.00),
        records=[Record("2025-12-01", "消费", -129.0, "抖音", "", 7908.97, 0, "")],
    )
    groups = build_groups([anchor, orphan, other])
    by_key = {g.key: g for g in groups}
    assert len(groups) == 2, [(g.key, len(g.records)) for g in groups]
    main = by_key["2023-01-01~2026-09-18"]
    assert len(main.records) == 4, "无汇总卡的图应按金额档并入锚点组"
    assert main.year_label == "2023–2026年", main.year_label
    assert main.period_label == "2023-01-01 至 2026-09-18"
    assert by_key["未知区间"].records[0].amount == -129.0
    assert by_key["未知区间"].year_label == "129元档", by_key["未知区间"].year_label
    assert by_key["未知区间"].pages[0].name == "129元档", by_key["未知区间"].pages[0].name
    # 缺口说明：差 59.00 = 1 笔 59 元
    assert main.expense == 177.00 and main.card_expense == 118.00
    assert main.gap_note.startswith("差 59.00 = 1 笔 59 元"), main.gap_note

    print("statement.selftest OK")


if __name__ == "__main__":
    selftest()
