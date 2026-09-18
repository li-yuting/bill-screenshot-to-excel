"""单笔交易详情页（一图一笔交易）的解析。

同一种信息有两种 App 版式，靠「版式档案」（``Profile``）区分：

| 版式 | 特征标签 | 摘要来源 |
|---|---|---|
| 收支详情单页 | 交易卡号 / 交易账户 / 交易户名 / 交易时间 / 业务摘要 / 交易场所 / 交易金额 | 「业务摘要」标签 |
| 建行「明细详情」 | 交易账户 / 交易时间 / 支付机构 / 商户名称 / 商品信息 / 交易方式 / 对方账户 / 记账日 / 账户余额 / 交易流水号 | 顶部大金额上方的短文本块 |

两者的共同点：字段区都是「标签在左、值右对齐」，所以取值统一走
「标签行带内、靠右的块按 (y, x) 拼接」—— 拼接是为了救**换行的长值**（交易流水号会断成两块，
只取最右一块会丢最后几位）。

**不使用绝对像素**（上一版把「顶部金额区」写死成 ``y < 550``，建行那批图的大金额在
``y = 543.7``，距离出界只差 6.3 像素，属于侥幸过关）：

- 字段区上沿 = 最靠上的**标签块**的 y
- 值区下沿 = 标签 y + ``VALUE_ZONE_RATIO`` × 相邻标签间距的**中位数**
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Protocol, Sequence

MONEY_RE = re.compile(r"([￥¥]?)\s*(-?)\s*([\d,]+)\.(\d{2})")
"""金额模式。判断「整块就是一个金额」用 fullmatch，取「余额：17,447.64」里的数用 search。

注意负号的位置：``-69.00``（负号在前）和 ``￥-129.00``（币符在前、负号在后）都要认。
老版本的正则把负号写死在开头，遇到后者直接匹配失败 —— 建行那批图就是这样被全数跳过的。
"""
DATETIME_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\D{0,3}(\d{1,2}):(\d{2}):(\d{2})")
DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
PHONE_RE = re.compile(r"电话?\s*\d{6,}$")
"""商户名后面被 OCR 粘上的客服电话（``好分期电话4000856608``），取商户名时剥掉。"""

ROW_GAP = 30
"""值相对标签允许往上偏的距离。"""
VALUE_ZONE_RATIO = 0.8
"""值区下沿 = 标签 y + 该比例 × 相邻标签间距中位数。"""
DEFAULT_PITCH = 110.0
"""标签太少时相邻间距的兜底值。"""
BOOL_EPS = 0.005


class TextBlock(Protocol):
    """OCR 文字块需要提供的最小结构（见 ocr.Item）。"""

    y: float
    y2: float
    x: float
    x2: float
    t: str

    @property
    def h(self) -> float: ...


@dataclass(frozen=True)
class Profile:
    """一种版式档案。"""

    key: str
    title: str
    labels: tuple[str, ...]
    """本版式的全部标签，用于识别版式、定位字段区上沿、以及判定缺字段。"""
    fields: dict[str, str]
    """标签 -> Detail 属性名；值为 ``""`` 表示这个标签不进核心字段。"""
    summary_label: str | None = None
    """摘要取自哪个标签；None 表示「顶部大金额上方的短文本块」。"""
    balance_label: str | None = None
    """余额取自哪个标签的值；None 表示全局搜含「余额」的块。"""
    booking_label: str | None = None
    """记账日取自哪个标签，用于和交易时间的日期交叉校验。"""


PROFILES: tuple[Profile, ...] = (
    Profile(
        key="souzhixing",
        title="收支详情单页",
        labels=(
            "交易卡号", "交易账户", "交易户名", "交易时间", "业务摘要", "交易场所", "交易金额",
        ),
        fields={
            "交易卡号": "card",
            "交易账户": "account",
            "交易户名": "holder",
            "交易时间": "time",
            "业务摘要": "summary",
            "交易场所": "place",
            "交易金额": "trade_amount",
        },
        summary_label="业务摘要",
    ),
    Profile(
        key="ccb-detail",
        title="建行「明细详情」",
        labels=(
            "交易账户", "交易时间", "支付机构", "商户名称", "商品信息",
            "交易方式", "对方账户", "记账日", "账户余额", "交易流水号",
        ),
        fields={
            "交易账户": "account",
            "交易时间": "time",
            # 渠道 + 商户拼成「交易场所」，和另一版式的「宝付-好分期」同构，
            # 这样「按交易场所」的汇总维度对两种版式都成立
            "支付机构": "place_channel",
            "商户名称": "place_merchant",
            "账户余额": "balance",
            "记账日": "booking",
        },
        balance_label="账户余额",
        booking_label="记账日",
    ),
)

PLACE_JOINER = " / "


@dataclass
class Detail:
    """一笔交易的明细详情。"""

    source: str
    copies: list[str] = field(default_factory=list)
    profile_key: str = ""
    profile_title: str = ""
    amount: float | None = None
    balance: float | None = None
    time: str = ""
    card: str = ""
    account: str = ""
    holder: str = ""
    summary: str = ""
    place: str = ""
    booking: str = ""
    trade_amount: float | None = None
    extra: dict[str, str] = field(default_factory=dict)
    """版式特有的字段（标签 -> 值），导出时按出现过的标签动态加列。"""
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
        return abs(abs(self.amount) - self.trade_amount) < BOOL_EPS

    @property
    def date_consistent(self) -> bool | None:
        """「记账日」是否等于「交易时间」的日期；缺一个字段时返回 None。

        建行版式没有「交易金额」这个字段，少了一个原本的交叉校验点，
        用「记账日 vs 交易时间」补上。
        """
        if not self.booking or not self.date:
            return None
        return self.booking == self.date


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _money(text: str) -> float | None:
    """整块文本就是一个金额时返回数值，否则 None。"""
    match = MONEY_RE.fullmatch(_norm(text))
    if not match:
        return None
    value = float(f"{match.group(3).replace(',', '')}.{match.group(4)}")
    return -value if match.group(2) == "-" else value


def _found_any(text: str, amount: float | None) -> float | None:
    """从任意文本里搜一个金额（用于「余额：17,447.64」这类带前缀的块）。"""
    found = MONEY_RE.search(_norm(text))
    if not found:
        return None
    value = float(f"{found.group(3).replace(',', '')}.{found.group(4)}")
    return -value if found.group(2) == "-" else value


def _stamp(raw: str) -> str:
    """``2025-06-11 11:50:25`` -> 补零后的标准写法；取不到则返回空串。"""
    found = DATETIME_RE.search(raw)
    if not found:
        return ""
    year, month, day, hour, minute, second = found.groups()
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
        f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
    )


def _plain_date(raw: str) -> str:
    found = DATE_RE.match(raw)
    if not found:
        return ""
    year, month, day = found.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _clean_merchant(text: str) -> str:
    """剥掉商户名后面被 OCR 粘上的客服电话。"""
    return PHONE_RE.sub("", text)


def detect_profile(items: Sequence[TextBlock]) -> tuple[Profile, int]:
    """按「命中的特征标签数」挑版式档案，取命中最多的那个。

    Args:
        items: 该图的全部 OCR 文字块。

    Returns:
        ``(档案, 命中标签数)``；命中数相同时取 ``PROFILES`` 里靠前的。
    """
    texts = [_norm(item.t) for item in items]
    best: tuple[Profile, int] = (PROFILES[0], 0)
    for profile in PROFILES:
        hits = sum(1 for label in profile.labels if any(label in text for text in texts))
        if hits > best[1]:
            best = (profile, hits)
    return best


def _label_items(items: Sequence[TextBlock], profile: Profile) -> tuple[dict, float]:
    """找出标签块，并算出相邻标签间距的中位数（作为值区高度的尺度）。"""
    found: dict[str, TextBlock] = {}
    for item in items:
        flat = _norm(item.t)
        for label in profile.labels:
            if label in flat and label not in found:
                found[label] = item
    ordered = sorted(found.values(), key=lambda it: it.y)
    gaps = [
        second.y - first.y
        for first, second in zip(ordered, ordered[1:])
        if second.y - first.y > 1
    ]
    return found, (statistics.median(gaps) if gaps else DEFAULT_PITCH)


def _value_of(
    items: Sequence[TextBlock],
    label_item: TextBlock,
    pitch: float,
    profiles: Sequence[Profile],
) -> str:
    """取一个标签的值：标签行带内、靠右的块按 (y, x) 拼起来。

    值右对齐，所以不能只按「标签右侧」判断，得看右边界；
    拼起来是为了救换行的长值（交易流水号会被断成两块，间隔还可能超过「同一行」的容差）。
    """
    zone_top = label_item.y - ROW_GAP
    zone_bottom = label_item.y + VALUE_ZONE_RATIO * pitch
    all_labels = tuple(label for profile in profiles for label in profile.labels)
    chunks = [
        item
        for item in items
        if item is not label_item
        and zone_top <= item.y < zone_bottom
        and item.x2 > label_item.x2
        and not any(label in _norm(item.t) for label in all_labels)
    ]
    chunks.sort(key=lambda it: (it.y, it.x))
    return "".join(_norm(item.t) for item in chunks)


def _field_top(items: Sequence[TextBlock], labels: Sequence[str]) -> float:
    """字段区上沿：最靠上的那个标签块的 y。没有标签时返回无穷大。"""
    tops = [item.y for item in items if any(label in _norm(item.t) for label in labels)]
    return min(tops) if tops else float("inf")


def _top_amount(items: Sequence[TextBlock], profile: Profile) -> float | None:
    """顶部大金额：字段区上沿之上，纯金额块里字号最大的那个。"""
    field_top = _field_top(items, profile.labels)
    candidates = [
        item
        for item in items
        if item.y < field_top
        and "余额" not in _norm(item.t)
        and _money(item.t) is not None
    ]
    if not candidates:
        return None
    return _money(max(candidates, key=lambda it: it.h).t)


def _summary_above(items: Sequence[TextBlock], amount: float | None, profile: Profile) -> str:
    """摘要取自顶部大金额**上方**最近的那个短文本块。

    建行版式把「消费 / 充值 / 消费退货」放在大金额上方，没有独立的「业务摘要」标签。
    取最靠下的那个候选（离金额最近），顺带把状态栏图标之类的噪声甩掉。
    """
    if amount is None:
        return ""
    field_top = _field_top(items, profile.labels)
    candidates = []
    for item in items:
        flat = _norm(item.t)
        if item.y >= field_top or item.h < 40 or any(ch.isdigit() for ch in flat):
            continue
        if any(label in flat for label in profile.labels) or flat in TITLE_WORDS:
            continue
        candidates.append(item)
    return _norm(max(candidates, key=lambda it: it.y).t) if candidates else ""


TITLE_WORDS = ("明细详情", "收支详情", "交易详情")


def _balance_of(
    items: Sequence[TextBlock],
    profile: Profile,
    label_items: dict,
    pitch: float,
) -> float | None:
    """余额：档案指定了标签就取该标签的值，否则全局搜含「余额」的块。"""
    if profile.balance_label and profile.balance_label in label_items:
        return _found_any(
            _value_of(items, label_items[profile.balance_label], pitch, PROFILES), None
        )
    for item in items:
        if "余额" in item.t:
            value = _found_any(item.t, None)
            if value is not None:
                return value
    return None


def parse_page(items: Sequence[TextBlock], source: str, copies: Sequence[str] = ()) -> Detail:
    """解析一张「单笔交易详情」截图。

    Args:
        items: 该图的全部 OCR 文字块。
        source: 代表文件名。
        copies: 内容相同的副本文件名。

    Returns:
        该笔交易的结构化结果；缺失的字段名会记在 ``missing`` 里，版式特有字段进 ``extra``。

    Raises:
        ValueError: 顶部金额没识别到，说明这不是一张单笔详情图。
    """
    profile, hits = detect_profile(items)
    detail = Detail(
        source=source,
        copies=list(copies),
        profile_key=profile.key,
        profile_title=profile.title,
    )
    detail.amount = _top_amount(items, profile)
    label_items, pitch = _label_items(items, profile)
    detail.balance = _balance_of(items, profile, label_items, pitch)

    values = {
        label: _value_of(items, item, pitch, PROFILES)
        for label, item in label_items.items()
    }

    place_channel = ""
    place_merchant = ""
    for label in profile.labels:
        raw = values.get(label, "")
        attr = profile.fields.get(label, "")
        if attr == "place_channel":
            place_channel = raw
        elif attr == "place_merchant":
            place_merchant = _clean_merchant(raw)
        elif attr == "time":
            detail.time = _stamp(raw)
        elif attr == "booking":
            detail.booking = _plain_date(raw)
        elif attr == "balance":
            detail.balance = _found_any(raw, detail.balance)
        elif attr == "trade_amount":
            detail.trade_amount = _money(raw)
        elif attr:
            setattr(detail, attr, raw)
        else:
            # 档案里没映射到核心字段的，进 extra，导出时按需加列
            detail.extra[label] = raw

    if place_channel or place_merchant:
        detail.place = PLACE_JOINER.join(part for part in (place_channel, place_merchant) if part)

    if profile.summary_label:
        detail.summary = values.get(profile.summary_label, "")
    else:
        detail.summary = _summary_above(items, detail.amount, profile)

    detail.missing = [label for label in profile.labels if not values.get(label)]
    if detail.amount is None:
        raise ValueError(
            f"{source}：没识别到金额（按「{profile.title}」版式解析，命中 {hits} 个标签），"
            f"可能不是单笔详情截图"
        )
    return detail


def dedupe(details: Sequence[Detail]) -> tuple[list[Detail], dict[str, str]]:
    """去掉「同一笔交易被多张截图记录」的重复项。

    同一笔交易在不同截图里，交易时间/金额/余额/账户必然完全相同；不同交易即使金额相同，
    交易后余额也不会相同（每笔都会改变余额），所以这几个字段足以判定是同一笔。

    Args:
        details: 全部解析结果。

    Returns:
        ``(去重后的明细, {重复截图名: 首次出现的代表文件名})``
    """
    seen: dict[tuple, str] = {}
    unique: list[Detail] = []
    repeats: dict[str, str] = {}
    for item in details:
        key = (
            item.time,
            round(item.signed_amount or 0.0, 2),
            item.balance,
            item.card or item.account,
        )
        first = seen.get(key)
        if first is not None:
            repeats[item.source] = first
            continue
        seen[key] = item.source
        unique.append(item)
    return unique, repeats


def extra_columns(details: Sequence[Detail]) -> list[str]:
    """按出现顺序列出「版式特有字段」的列名（只保留至少有一条非空值的）。"""
    ordered: list[str] = []
    for item in details:
        for label in item.extra:
            if label not in ordered:
                ordered.append(label)
    return [label for label in ordered if any(item.extra.get(label) for item in details)]


def summaries_of(details: Sequence[Detail]) -> dict[str, object]:
    """汇总几项用于校对的事实：金额/日期一致性、重复、账户唯一性。"""
    inconsistent = [d.source for d in details if d.amount_consistent is False]
    unknown = [d.source for d in details if d.amount_consistent is None]
    date_bad = [d.source for d in details if d.date_consistent is False]
    date_checked = [d for d in details if d.date_consistent is not None]
    missing = {d.source: d.missing for d in details if d.missing}
    return {
        "inconsistent": inconsistent,
        "unknown": unknown,
        "date_inconsistent": date_bad,
        "date_checked": len(date_checked),
        "missing": missing,
        "cards": sorted({d.card for d in details if d.card}),
        "accounts": sorted({d.account for d in details if d.account}),
        "holders": sorted({d.holder for d in details if d.holder}),
        "profiles": sorted({d.profile_title for d in details if d.profile_title}),
    }


def selftest() -> None:
    """纯函数自检：不依赖 OCR。样例数据全部为构造值，不含任何真实账户信息。"""

    class _T:
        def __init__(self, y, y2, x, x2, t):
            self.y, self.y2, self.x, self.x2, self.t = y, y2, x, x2, t

        @property
        def h(self):
            return self.y2 - self.y

    # ---------- 版式一：收支详情单页 ----------
    items = [
        _T(38, 74, 60, 178, "12:50"),
        _T(42, 63, 922, 1044, "089%"),
        _T(155, 196, 437, 666, "收支详情"),
        _T(373, 426, 415, 719, "-69.00"),
        _T(483, 519, 389, 717, "余额：11,111.11"),
        _T(561, 591, 41, 214, "交易卡号"),
        _T(562, 591, 771, 1059, "6222****0000"),
        _T(665, 694, 41, 212, "交易账户"),
        _T(666, 695, 769, 1060, "3100****0000"),
        _T(767, 796, 969, 1062, "张三"),
        _T(769, 803, 41, 212, "交易户名"),
        _T(875, 906, 43, 212, "交易时间"),
        _T(877, 913, 624, 1055, "2026-02-0112:10:31"),
        _T(976, 1009, 967, 1062, "消费"),
        _T(979, 1014, 44, 212, "业务摘要"),
        _T(1080, 1110, 40, 215, "交易场所"),
        _T(1082, 1119, 827, 1059, "某支付-某商户"),
        _T(1188, 1219, 939, 1061, "69.00"),
        _T(1189, 1223, 43, 214, "交易金额"),
        _T(1317, 1350, 450, 649, "查看全部"),
    ]

    assert detect_profile(items)[0].key == "souzhixing"
    detail = parse_page(items, "a.png")
    assert detail.profile_key == "souzhixing"
    assert detail.amount == -69.00, detail.amount
    assert detail.balance == 11111.11, detail.balance
    assert detail.time == "2026-02-01 12:10:31", detail.time
    assert (detail.date, detail.clock, detail.year, detail.month) == (
        "2026-02-01", "12:10:31", "2026", "2026-02",
    )
    assert detail.card == "6222****0000" and detail.account == "3100****0000"
    assert detail.holder == "张三" and detail.summary == "消费"
    assert detail.place == "某支付-某商户" and detail.trade_amount == 69.00
    assert detail.missing == [], detail.missing
    assert detail.amount_consistent is True
    assert detail.signed_amount == -69.00
    assert detail.date_consistent is None, "该版式没有记账日，交叉校验应为 None"
    assert detail.extra == {}, detail.extra

    # 长场所名（右对齐仍要取到）、收入（正号）
    items2 = list(items)
    items2[16] = _T(1082, 1119, 484, 1045, "某支付-某商户（代扣）")
    items2[3] = _T(373, 426, 387, 746, "179.00")
    items2[17] = _T(1188, 1219, 937, 1061, "179.00")
    detail2 = parse_page(items2, "b.png")
    assert detail2.place == "某支付-某商户（代扣）", detail2.place
    assert detail2.amount == 179.00 and detail2.signed_amount == 179.00
    assert detail2.amount_consistent is True

    # ---------- 版式二：建行「明细详情」 ----------
    def ccb_items(amount="-129.00", trade="2024-12-14 17:59:30", booking="2024-12-14",
                  serial_tail="6"):
        return [
            _T(54.5, 103.2, 126.9, 260.7, "16:58"),
            _T(58.0, 102.1, 758.0, 888.4, "三》"),
            _T(190.1, 245.7, 432.2, 628.8, "明细详情"),
            _T(412.7, 469.5, 480.2, 579.7, "消费"),
            # 币符在前、负号在后 —— 老正则就是不认这种写法
            _T(543.7, 609.8, 361.3, 699.7, f"￥{amount}"),
            _T(777.9, 825.4, 64.0, 226.4, "交易账户"),
            _T(779.0, 820.7, 593.4, 993.5, "建设银行6217***0000"),
            _T(891.5, 939.0, 64.0, 227.5, "交易时间"),
            _T(895.0, 935.6, 607.1, 993.5, trade),
            _T(1005.1, 1052.6, 65.2, 227.5, "支付机构"),
            _T(1007.4, 1049.1, 440.2, 990.1, "某支付网络科技（上海）有限公司"),
            _T(1116.4, 1166.2, 870.1, 995.8, "某商户电话4000856608"),
            _T(1117.5, 1165.0, 66.3, 229.8, "商户名称"),
            _T(1228.8, 1281.0, 908.9, 999.3, "收款"),
            _T(1231.1, 1278.6, 65.2, 227.5, "商品信息"),
            _T(1342.4, 1393.4, 62.9, 229.8, "交易方式"),
            _T(1342.4, 1393.4, 830.0, 997.0, "快捷支付"),
            _T(1460.7, 1502.4, 500.8, 992.4, "某***信息科技有限公司"),
            _T(1485.0, 1532.5, 64.0, 226.4, "对方账户"),
            _T(1511.7, 1558.1, 759.2, 995.8, "Z******0015"),
            _T(1625.3, 1676.3, 62.9, 187.5, "记账日"),
            _T(1632.3, 1670.6, 772.9, 994.7, booking),
            _T(1738.9, 1786.4, 62.9, 228.7, "账户余额"),
            _T(1743.5, 1784.1, 879.2, 997.0, "￥0.31"),
            # 流水号换行：头一段在标签上方、尾一段在标签下方，间隔 32px > ROW_GAP
            _T(1859.5, 1894.3, 356.7, 991.3, f"202412141000007159214010030110"),
            _T(1882.7, 1930.2, 65.2, 265.2, "交易流水号"),
            _T(1915.1, 1953.4, 967.2, 995.8, serial_tail),
            _T(2106.4, 2157.4, 64.0, 152.1, "备注"),
            _T(2195.7, 2242.1, 66.3, 263.0, "记录点什么"),
        ]

    assert detect_profile(ccb_items())[0].key == "ccb-detail"
    ccb = parse_page(ccb_items(), "c.png")
    assert ccb.profile_key == "ccb-detail"
    assert ccb.amount == -129.00, ccb.amount
    assert ccb.summary == "消费", ccb.summary
    assert ccb.time == "2024-12-14 17:59:30", ccb.time
    assert ccb.account == "建设银行6217***0000", ccb.account
    assert ccb.balance == 0.31, ccb.balance
    assert ccb.booking == "2024-12-14" and ccb.date_consistent is True
    assert ccb.place == "某支付网络科技（上海）有限公司 / 某商户", ccb.place
    assert ccb.trade_amount is None and ccb.amount_consistent is None
    assert ccb.extra["交易方式"] == "快捷支付", ccb.extra
    assert ccb.extra["商品信息"] == "收款"
    # 对方账户两行要拼起来；流水号换行的尾段（"6"）不能丢
    assert ccb.extra["对方账户"] == "某***信息科技有限公司Z******0015", ccb.extra["对方账户"]
    assert ccb.extra["交易流水号"].endswith("01106"), ccb.extra["交易流水号"]
    assert ccb.missing == [], ccb.missing

    # 收入（正号）与记账日不一致都要能被发现
    ccb2 = parse_page(ccb_items(amount="129.00", booking="2024-12-15"), "d.png")
    assert ccb2.amount == 129.00 and ccb2.signed_amount == 129.00
    assert ccb2.date_consistent is False, ccb2.date_consistent

    # ---------- 交叉：比对与去重 ----------
    broken = [it for it in items if _norm(it.t) != "业务摘要"]
    for index, item in enumerate(broken):
        if _norm(item.t) == "69.00":
            broken[index] = _T(1188, 1219, 939, 1061, "99.00")
            break
    detail3 = parse_page(broken, "e.png")
    detail3.balance = 111.11  # 拉开与 a.png 的差值，否则会被判成同一笔
    assert detail3.missing == ["业务摘要"], detail3.missing
    assert detail3.amount == -69.00 and detail3.trade_amount == 99.00
    assert detail3.amount_consistent is False

    report = summaries_of([detail, detail2, detail3, ccb, ccb2])
    assert report["inconsistent"] == ["e.png"], report["inconsistent"]
    assert report["date_inconsistent"] == ["d.png"], report["date_inconsistent"]
    assert report["date_checked"] == 2, report["date_checked"]
    assert report["cards"] == ["6222****0000"]
    assert report["profiles"] == ["建行「明细详情」", "收支详情单页"], report["profiles"]

    assert extra_columns([ccb, ccb2]) == [
        "商品信息", "交易方式", "对方账户", "交易流水号",
    ], extra_columns([ccb, ccb2])
    assert extra_columns([detail, detail2]) == []

    twin = parse_page(items, "copy.png")
    unique, repeats = dedupe([detail, twin, detail2, detail3])
    assert len(unique) == 3, [d.source for d in unique]
    assert repeats == {"copy.png": "a.png"}, repeats
    assert unique[0].source == "a.png"

    # 不是详情页时要明确报错，而不是产出一堆空字段
    try:
        parse_page([_T(10, 40, 10, 200, "与账单无关的一张图")], "f.png")
    except ValueError:
        pass
    else:
        raise AssertionError("没有金额时应当抛 ValueError")

    print("detail.selftest OK")


if __name__ == "__main__":
    selftest()
