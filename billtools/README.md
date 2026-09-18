# 账单截图转 Excel 控制台（billtools）

一个入口，两种账单截图，启动台里选一个再转换。

| 工具 | 适用截图 | 输出 |
|---|---|---|
| 好分期账单长截图 | 手机多段滚动长截图（宽 1224，高可达 4 万像素，同一年份可能拆成多张） | `汇总` + `全部明细(去重)` + 每张唯一截图一个工作表 |
| 收支详情单页 | 手机单屏「收支详情」页（一张图 = 一笔交易） | `明细` + `汇总`（按年/按月/按金额档/按摘要/按交易场所 + 自动校验） |

两者都会按内容 MD5 去重、按高亮金额给整行标黄。

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
| `ocr.py` | 扫描目录、按内容 MD5 去重、OCR；长图自动分块（3000/2600 + 尾部 2400 补识别），单屏图一次识别 |
| `statement.py` | 工具一：长截图 → 记录；多段合并去重；用顶部汇总卡勾稽（仅标准库） |
| `detail.py` | 工具二：收支详情单页 → 一笔交易（仅标准库） |
| `excel.py` | 共用单元格样式 + `write_statement` / `write_detail` 两个导出函数 |
| `build_exe.py` / `run.bat` / `app.ico` | 打包脚本 / 双击启动 / 图标 |

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

- 只适配这两类版式。换 App 要改 `statement.py` / `detail.py` 里的正则与坐标阈值。
- 「收支详情」页的「查看全部 ∨」是折叠状态，展开才有对方户名、流水号，当前取不到。
- OCR 单线程：一张 4 万像素长图约 40 秒，43 张单屏图约 90 秒。
- 长截图拼接可能漏抓个别交易：明细合计与页面汇总卡对不上时，汇总表会标红给出差额。
