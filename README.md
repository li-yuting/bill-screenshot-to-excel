# bill-screenshot-to-excel

把手机银行 / 借款 App 的账单截图，用**本地 OCR** 转成结构化 Excel。
全程离线，截图不上传任何服务器。

包含一个工具：

| 目录 | 工具 | 输入 | 输出 |
|---|---|---|---|
| `billtools/` | 账单截图转 Excel（长截图 + 收支详情单页） | 截图目录 | `账单明细_*.xlsx` / `收支详情明细_*.xlsx` |

> 早期还有一版只支持长截图的独立程序（`bill2excel`），功能已完全并入 `billtools`，不再单独维护。

## 输入长什么样

`billtools/` 认两类截图（都已脱敏，原图不入库）：

- **账单列表长截图** —— 一张超长的交易列表截图（好分期约 1224 宽，建行「账户明细」经企业微信转发后只有 640 宽），顶部有吸顶的「支出/收入」汇总卡。也认「图片查看器」窗口的单屏截图（同一列表的局部放大）
- **收支详情单屏图** —— 单笔交易的详情页（1102×2430），字段固定：交易卡号 / 交易账户 / 交易户名 / 交易时间 / 业务摘要 / 交易场所 / 交易金额

![列表长截图](samples/example-list-longshot.png)
![收支详情单屏图](samples/example-detail-screen.png)

> 样例图已做脱敏：所有金额、日期、时间、卡号、余额、户名、商户名都用灰块遮蔽。
> 仓库内**不含任何真实账单数据**，规则见 `.gitignore`（`账单*/`、`outputs/`、`*.xlsx` 一律不入库）。

### 画了红框的行会被标红

如果你在截图里用红框圈出某些行（表示"这几行要特别核对"），程序会自己认出这些框，
在 Excel 里给对应行加**红色底纹**（不额外加列）。红框是按「描边矩形」的形状认出来的 ——
内部是空的才算，手机状态栏里那些实心的橙红 App 图标不会被误认。

## 技术要点

- **OCR**：[`rapidocr-onnxruntime`](https://github.com/RapidAI/RapidOCR)（PP-OCRv4，离线）
- **图片处理**：Pillow + NumPy
- **表格**：openpyxl
- **界面**：tkinter（可打包成单文件 exe）
- **定位方式：自校准，不写死分辨率**。手机型号、系统缩放、企业微信转发压缩、图片查看器的显示比例都不可预设，所以解析器不认坐标常量，而是从**每张图自己**的金额块里量出行距、字号、金额列起点。换手机、换截图工具都不用改代码
- **界面噪声过滤**：单屏截图里的窗口标题栏、工具栏箭头、`[1:1]` 缩放标记既无汉字也无字母，会被忽略 —— 否则它们会被当成渠道名，把跨图去重搞坏
- **金额清洗**：中文标点归一化 + 千分位逗号被 OCR 读成句点的修复（`4.290.70` → `4,290.70`）
- **去重**：截图内容 MD5 去重 + 语义去重（跨图），键为「日期 + 类型 + 金额 + 余额（缺失时用渠道）+ 时间」。余额与渠道都没读到时按「宁可重复也不丢失」保留并标注
- **校验**：解析结果与页面汇总卡逐分比对；差额能被主要金额档整除时，直接说明"差 N 笔 M 元"

## 快速开始

前置：Python 3.11+（需要带 tkinter 的版本，GUI 依赖它）。

```bash
pip install rapidocr-onnxruntime openpyxl pillow

python billtools/app.py                        # 图形界面
python billtools/app.py statement <截图目录>     # 命令行：账单列表长截图
python billtools/app.py detail <截图目录>        # 命令行：收支详情单页
python billtools/app.py --selftest statement <截图目录>   # 自检（纯函数断言 + 跑一遍并核对）
```

`statement.py` / `ocr.py` 可以单独跑纯函数自检，不需要任何图片：

```bash
python billtools/statement.py     # 解析、自校准、红框落行、去重
python billtools/ocr.py           # 红框检测（描边矩形 vs 实心色块）
```

打包单文件 exe：

```bash
python billtools/build_exe.py                  # 产物 billtools/dist/账单截图转Excel.exe
python billtools/smoke_test.py                 # 打包后冒烟：自检 + 确认 GUI 真起窗
```

> `build_exe.py` 退出码 0 **不代表产物存在**（本机实测会被终端安全软件拦掉），所以打完包务必跑 `smoke_test.py`。

## 文档

- `docs/superpowers/specs/2026-09-17-bill2excel-design.md`
- `docs/superpowers/specs/2026-09-18-bill-console-design.md`
- `docs/superpowers/specs/2026-09-18-billtools-console-design.md`
- `docs/superpowers/specs/2026-09-18-bill-statement-selfcalib-design.md` —— 自校准定位与红框标注

## 免责声明

本项目仅用于把**本人**的账单截图整理成表格，方便对账。请遵守相关服务条款与法律法规，不要用它处理你无权处理的他人数据。
