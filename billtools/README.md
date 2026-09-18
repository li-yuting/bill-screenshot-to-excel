# 账单截图转 Excel 控制台（billtools）

一个入口，两种账单截图，启动台里选一个再转换。

| 工具 | 适用截图 | 输出 |
|---|---|---|
| 账单列表长截图 | 手机滚动长截图（好分期约 1224 宽，建行「账户明细」只有 640 宽）以及「图片查看器」窗口的单屏截图 | `汇总` + `全部明细(去重)` + 每张唯一截图一个工作表 |
| 收支详情单页 | 手机单屏「收支详情」页（一张图 = 一笔交易） | `明细` + `汇总`（按年/按月/按金额档/按摘要/按交易场所 + 自动校验） |

两者都会按内容 MD5 去重、按高亮金额给整行标黄；
如果你在截图里用**红框**圈了行（表示要特别核对），工具一会自动认出并给整行加**红色底纹**。

## 定位方式：自校准，不写死分辨率

手机型号、系统缩放、企业微信转发时的压缩、图片查看器的显示比例都无法预设，
所以 `statement.py` **不使用任何绝对像素阈值**。它从每张图**自己**的金额块里量出三个量：

| 量出的量 | 含义 | 取代了原来的 |
|---|---|---|
| `row_pitch` | 相邻金额块的 y 间距中位数 | 「余额在金额下方 170px」 |
| `money_h` | 金额块高度中位数 | 「金额字号 ≥ 30」「交易类型字号 40~75」 |
| `money_x` | 金额块左边界中位数 | 「金额必须在 x > 600」「交易类型必须在 x < 400」 |

依据是两条与分辨率无关的性质：金额块的**文本形状**（正则），以及
**右对齐 + 单行 + 等距排布**（版式）。实测同一批账单里三张图量出 214/92/113 的行距、
33/19/21 的字号，一套代码全部正确；账单1（1224 宽）的回归结果与旧解析器逐分相同。

**要换 App 版式**，改的是正则（`AMOUNT_RE` / `DATE_RE` / `BALANCE_RE`），不用再调坐标常量。

## 红框标注怎么认出来的

红框是画在图片上的描边矩形。程序取红色像素掩码 → 按纵向间隔聚类 → 每个簇的包围盒算一个框。
关键是**别把手机状态栏里的橙红 App 图标当成标注框**（实测每张图都会被误判一个），
区分靠「内部红占比」：

| | 内部红占比 | 形状 |
|---|---|---|
| 手绘标注框 | 0.000 | 描边矩形，内部只有黑字 |
| 状态栏 App 图标 | 0.344 | 实心色块 |

判据取 ≤ 0.15。不按颜色阈值过滤 —— 用户用什么颜色的笔不该被预设，而"描边 vs 实心"与笔的颜色无关。

## 先决条件

Python 必须带 tkinter（图形界面需要）。本机用的是：

```
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311
```

重建环境：

```bash
D:\ucredit\liyuting\AppData\Local\Programs\Python\Python311\python.exe -m venv D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\pip install rapidocr-onnxruntime openpyxl pillow
```

## 使用

**双击 `run.bat`** → 启动台 → 点一张卡片 → 选文件夹 → 开始转换。

命令行：

```bash
python app.py                                  # 打开控制台
python app.py detail    "D:\图片目录"           # 直接跑「收支详情单页」
python app.py statement "D:\图片目录" --highlight "59,69,179,199" --out "D:\结果.xlsx"
python app.py --selftest detail "D:\图片目录"    # 自检：纯函数断言 + 跑一遍并核对
```

跑之前确认目录里没有无关的临时截图——目录下所有 png/jpg 都会参与识别。

## 去重规则（工具二「收支详情单页」）

1. **内容完全相同**的图片按 MD5 去重（同一张图存了两份）
2. **交易时间 + 金额 + 交易后余额 + 卡号** 四项都相同 → 判定为同一笔交易被重复截图，合并
   （不同交易即使金额相同，余额也不会相同，所以这四项足以判定）
3. 两类重复都会在「汇总 → 自动校验」里列明是哪些文件

实测这批：45 个文件 → 43 张唯一截图 → **37 笔交易**（6 笔被重复截了两次）。

## 模块

| 文件 | 职责 |
|---|---|
| `app.py` | 控制台：启动台（卡片选择）+ 操作页 + 可折叠日志；同时是 CLI 与 `--selftest` 入口 |
| `ocr.py` | 扫描目录、按内容 MD5 去重、OCR、红色标注框检测；长图自动分块（3000/2600 + 尾部 2400 补识别），单屏图一次识别 |
| `statement.py` | 工具一：列表长截图 → 记录（自校准定位）；跨图去重 + 归组 + 勾稽（仅标准库） |
| `detail.py` | 工具二：收支详情单页 → 一笔交易（仅标准库） |
| `excel.py` | 共用单元格样式 + `write_statement` / `write_detail` 两个导出函数 |
| `build_exe.py` / `smoke_test.py` / `run.bat` / `app.ico` | 打包脚本 / 打包后冒烟 / 双击启动 / 图标 |

`statement.py` 和 `ocr.py` 都能单独跑纯函数自检，不需要图片：

```bash
python statement.py     # 自校准、解析、红框落行、去重、日期回填
python ocr.py           # 红框检测：描边矩形 vs 实心色块
```

## 打包成 exe

```bash
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\pip install pyinstaller
cd /d D:\ucredit\liyuting\Desktop\转表格\billtools
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\python.exe build_exe.py
```

产物：`dist\账单截图转Excel.exe`，**102 MB 单文件**，双击即用。冷启动要先把内容解包到临时目录，
第一次打开约 10~30 秒，之后快一些。必须用带 tkinter 的环境打包，否则 exe 里没有 tcl/tk。

**⚠️ 本机打不出 `--onedir`**：PyInstaller 的 onedir 引导程序写进 `build\<名字>\<名字>.exe` 后会消失，
COLLECT 阶段报 `WARNING: Ignoring non-existent resource`，产物里只剩 `_internal`。同样参数换
`--onefile` 完全正常（已实测能跑、GUI 能起窗）。疑似终端安全软件所为——这台机器上跑着
`SECOMN64/SECOCL64`（亚信安全）、`safeSrv`、`tbGuard`、`QzhddrGuard`；
onedir 版本以前打出来过（`bill2excel\dist\账单转表格\`），后来连旧 exe 带整个 dist 一起不见了，
Defender 里没有拦截记录（多半是第三方终端安全软件静默清理的，那家的日志 Defender 看不到）。
如果要长期稳定地出 onedir 包，找 IT 把 `转表格` 目录加进信任区/排除项。

## 已知限制

- 版式识别靠正则 + 自校准，**不再需要调坐标常量**；换 App 主要改正则
  （`AMOUNT_RE` / `DATE_RE` / `BALANCE_RE` / `EXPENSE_RE` / `INCOME_RE`）。
- 「收支详情」页的「查看全部 ∨」是折叠状态，展开才有对方户名、流水号，当前取不到。
- OCR 单线程：一张 4 万像素长图约 40 秒，7 张长图约 4~7 分钟，3 张单屏图约 40 秒。
  两个 OCR 并行会互抢 CPU。
- 长截图拼接可能漏抓个别交易：明细合计与页面汇总卡对不上时，
  汇总表会给出「差 N 笔 M 元」的可解释说明，并提示补截该期间。
- 段首被吸顶汇总卡遮住的那一行日期为空，要靠同期间其它段回填；
  实在回填不到就显示「未知」，**不按相邻行猜**。
- 红框标注跨图不去重：同一笔若在两张图上都被圈，会两处标红（这是对的，你圈了两次）。
