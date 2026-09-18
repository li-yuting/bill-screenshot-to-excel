"""收支详情单页（一图一笔交易）的解析。

页面版式：顶部一行大金额（支出为负）+ 余额，下面固定 7 行「标签 —— 值」，
值一律右对齐（x2 ≈ 1060），所以按「同一行里最靠右的那个块」取值最稳。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence

MONEY_RE = re.compile(r"(-?)\s*[￥¥]?\s*([\d,]+)\.(\d{2})")
"""金额模式。判断「整块就是一个金额」用 fullmatch，取「余额：17,447.64」里的数用 search。"""
DATETIME_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\D{0,3}(\d{1,2}):(\d{2}):(\d{2})")
LABELS = ("交易卡号", "交易账户", "交易户名", "交易时间", "业务摘要", "交易场所", "交易金额")

TOP_ZONE = 550
"""顶部大金额所在的纵向区域：在标题下方、字段表上方。"""
ROW_GAP = 30
"""判断「同一行」的纵向容差。"""


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
class Detail:
    """一笔交易的收支详情。"""

    source: str
    copies: list[str] = field(default_factory=list)
    amount: float | None = None
    balance: float | None = None
    time: str = ""
    card: str = ""
    account: str = ""
    holder: str = ""
    summary: str = ""
    place: str = ""
    trade_amount: float | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def date(self) -> str:
        return self.time[:10]

    @property
    def clock(self) -> str:
        return self.time[11:]

    @property
    def year(self) -> str:
        return self.date[:4]

    @property
    def month(self) -> str:
        return self.date[:7]

    @property
    def signed_amount(self) -> float | None:
        """顶部大金额（支出为负、收入为正）；取不到时用「交易金额」按负号补。"""
        if self.amount is not None:
            return round(self.amount, 2)
        if self.trade_amount is not None:
            return round(-self.trade_amount, 2)
        return None

    @property
    def amount_consistent(self) -> bool | None:
        """「交易金额」是否等于顶部大金额的绝对值；缺一个字段时返回 None。"""
        if self.amount is None or self.trade_amount is None:
            return None
        return abs(abs(self.amount) - self.trade_amount) < 0.005


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _money(text: str) -> float | None:
    match = MONEY_RE.fullmatch(_norm(text))
    if not match:
        return None
    value = float(f"{match.group(2).replace(',', '')}.{match.group(3)}")
    return -value if match.group(1) == "-" else value


def _top_amount(items: Sequence[TextBlock]) -> float | None:
    """顶部大金额：纵向区域内的纯金额块里，字号最大的那个。"""
    candidates = [
        item
        for item in items
        if item.y < TOP_ZONE and "余额" not in item.t and _money(item.t) is not None
    ]
    if not candidates:
        return None
    return _money(max(candidates, key=lambda it: it.h).t)


def _balance(items: Sequence[TextBlock]) -> float | None:
    for item in items:
        if "余额" not in item.t:
            continue
        found = MONEY_RE.search(_norm(item.t))
        if found:
            value = float(f"{found.group(2).replace(',', '')}.{found.group(3)}")
            return -value if found.group(1) == "-" else value
    return None


def _value_of(items: Sequence[TextBlock], label_item: TextBlock) -> str:
    """取同一行里最靠右的文字块作为 label 的值。

    值右对齐，所以不能只按「标签右侧」取，得按右边界排序。
    """
    same_row = [
        item
        for item in items
        if item is not label_item
        and abs(item.y - label_item.y) <= ROW_GAP
        and item.x2 > label_item.x2
        and _norm(item.t) not in LABELS
    ]
    if not same_row:
        return ""
    return _norm(max(same_row, key=lambda it: it.x2).t)


def parse_page(items: Sequence[TextBlock], source: str, copies: Sequence[str] = ()) -> Detail:
    """解析一张「收支详情」截图。

    Args:
        items: 该图的全部 OCR 文字块。
        source: 代表文件名。
        copies: 内容相同的副本文件名。

    Returns:
        该笔交易的结构化结果；缺失的字段名会记在 ``missing`` 里。

    Raises:
        ValueError: 顶部金额与「交易金额」都没识别到，说明这不是一张收支详情图。
    """
    detail = Detail(source=source, copies=list(copies))
    detail.amount = _top_amount(items)
    detail.balance = _balance(items)

    label_items = {}
    for item in items:
        flat = _norm(item.t)
        for label in LABELS:
            if label in flat and label not in label_items:
                label_items[label] = item

    values = {label: _value_of(items, item) for label, item in label_items.items()}
    detail.card = values.get("交易卡号", "")
    detail.account = values.get("交易账户", "")
    detail.holder = values.get("交易户名", "")
    detail.summary = values.get("业务摘要", "")
    detail.place = values.get("交易场所", "")

    raw_time = values.get("交易时间", "")
    found = DATETIME_RE.search(raw_time)
    if found:
        year, month, day, hour, minute, second = found.groups()
        detail.time = (
            f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
            f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
        )
    detail.trade_amount = _money(values.get("交易金额", ""))

    detail.missing = [label for label in LABELS if not values.get(label)]
    if detail.amount is None and detail.trade_amount is None:
        raise ValueError(f"{source}：没识别到金额，可能不是「收支详情」截图")
    return detail


def dedupe(details: Sequence[Detail]) -> tuple[list[Detail], dict[str, str]]:
    """去掉「同一笔交易被多张截图记录」的重复项。

    同一笔交易在不同截图里，交易时间/金额/余额/卡号必然完全相同；不同交易即使金额相同，
    交易后余额也不会相同（每笔都会改变余额），所以这四个字段足以判定是同一笔。

    Args:
        details: 全部解析结果。

    Returns:
        ``(去重后的明细, {重复截图名: 首次出现的代表文件名})``
    """
    seen: dict[tuple, str] = {}
    unique: list[Detail] = []
    repeats: dict[str, str] = {}
    for item in details:
        key = (item.time, round(item.signed_amount or 0.0, 2), item.balance, item.card)
        first = seen.get(key)
        if first is not None:
            repeats[item.source] = first
            continue
        seen[key] = item.source
        unique.append(item)
    return unique, repeats


def summaries_of(details: Sequence[Detail]) -> dict[str, object]:
    """汇总几项用于校对的事实：金额一致性、重复、卡号唯一性。"""
    inconsistent = [d.source for d in details if d.amount_consistent is False]
    unknown = [d.source for d in details if d.amount_consistent is None]
    missing = {d.source: d.missing for d in details if d.missing}
    cards = sorted({d.card for d in details if d.card})
    accounts = sorted({d.account for d in details if d.account})
    holders = sorted({d.holder for d in details if d.holder})
    return {
        "inconsistent": inconsistent,
        "unknown": unknown,
        "missing": missing,
        "cards": cards,
        "accounts": accounts,
        "holders": holders,
    }


def selftest() -> None:
    """纯函数自检：不依赖 OCR，验证取值、时间解析与一致性判断。"""

    class _T:
        def __init__(self, y, y2, x, x2, t):
            self.y, self.y2, self.x, self.x2, self.t = y, y2, x, x2, t

        @property
        def h(self):
            return self.y2 - self.y

    items = [
        _T(38, 74, 60, 178, "12:50"),
        _T(42, 63, 922, 1044, "089%"),
        _T(155, 196, 437, 666, "收支详情"),
        _T(373, 426, 415, 719, "-69.00"),
        _T(483, 519, 389, 717, "余额：17,447.64"),
        _T(561, 591, 41, 214, "交易卡号"),
        _T(562, 591, 771, 1059, "6222****1489"),
        _T(665, 694, 41, 212, "交易账户"),
        _T(666, 695, 769, 1060, "3100****1510"),
        _T(767, 796, 969, 1062, "于翔"),
        _T(769, 803, 41, 212, "交易户名"),
        _T(875, 906, 43, 212, "交易时间"),
        _T(877, 913, 624, 1055, "2026-02-0112:10:31"),
        _T(976, 1009, 967, 1062, "消费"),
        _T(979, 1014, 44, 212, "业务摘要"),
        _T(1080, 1110, 40, 215, "交易场所"),
        _T(1082, 1119, 827, 1059, "宝付-好分期"),
        _T(1188, 1219, 939, 1061, "69.00"),
        _T(1189, 1223, 43, 214, "交易金额"),
        _T(1317, 1350, 450, 649, "查看全部"),
    ]

    detail = parse_page(items, "a.png")
    assert detail.amount == -69.00, detail.amount
    assert detail.balance == 17447.64, detail.balance
    assert detail.time == "2026-02-01 12:10:31", detail.time
    assert (detail.date, detail.clock, detail.year, detail.month) == (
        "2026-02-01", "12:10:31", "2026", "2026-02",
    )
    assert detail.card == "6222****1489" and detail.account == "3100****1510"
    assert detail.holder == "于翔" and detail.summary == "消费"
    assert detail.place == "宝付-好分期" and detail.trade_amount == 69.00
    assert detail.missing == [], detail.missing
    assert detail.amount_consistent is True
    assert detail.signed_amount == -69.00

    # 长场所名（右对齐仍要取到）、收入（正号）
    items2 = list(items)
    items2[16] = _T(1082, 1119, 484, 1045, "京东支付-北京好还科技（代扣）")
    items2[3] = _T(373, 426, 387, 746, "179.00")
    items2[17] = _T(1188, 1219, 937, 1061, "179.00")
    detail2 = parse_page(items2, "b.png")
    assert detail2.place == "京东支付-北京好还科技（代扣）", detail2.place
    assert detail2.amount == 179.00 and detail2.signed_amount == 179.00
    assert detail2.amount_consistent is True

    # 缺字段与不一致都要能被发现
    broken = [it for it in items if _norm(it.t) != "业务摘要"]
    for index, item in enumerate(broken):
        if _norm(item.t) == "69.00":
            broken[index] = _T(1188, 1219, 939, 1061, "99.00")
            break
    detail3 = parse_page(broken, "c.png")
    detail3.balance = 111.11  # 拉开与 a.png 的差值，否则它会被判成同一笔（专测缺字段+金额不一致）
    assert detail3.missing == ["业务摘要"], detail3.missing
    assert detail3.amount == -69.00 and detail3.trade_amount == 99.00
    assert detail3.amount_consistent is False

    report = summaries_of([detail, detail2, detail3])
    assert report["inconsistent"] == ["c.png"], report["inconsistent"]
    assert report["cards"] == ["6222****1489"] and report["holders"] == ["于翔"]

    # 同一笔交易被两张截图记录（文件名不同、内容一致）：必须去重
    twin = parse_page(items, "copy.png")
    unique, repeats = dedupe([detail, twin, detail2, detail3])
    assert len(unique) == 3, [d.source for d in unique]
    assert repeats == {"copy.png": "a.png"}, repeats
    assert unique[0].source == "a.png"

    print("detail.selftest OK")


if __name__ == "__main__":
    selftest()
