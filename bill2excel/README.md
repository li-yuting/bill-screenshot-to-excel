# 账单长截图 → Excel（bill2excel）

把手机账单的长截图（一次几十张 png，含内容完全相同的副本）自动转成 Excel：
**每张唯一截图一个工作表 + 汇总表 + 全部明细**，并用页面顶部汇总卡（支出/收入）与明细求和逐分核对。

## 安装依赖

**先决条件：Python 必须带 tkinter**（图形界面需要）。WorkBuddy 托管的 Python 3.13 不带 tkinter，
所以本机用带 tkinter 的系统 Python 3.11 单独建了一个环境：

```bash
# 本机已建好：D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311
D:\ucredit\liyuting\AppData\Local\Programs\Python\Python311\python.exe -m venv D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\pip install rapidocr-onnxruntime openpyxl pillow
```

新机器上重建环境就照上面两行；只要 `import tkinter` 能过，随便哪个 Python 都行。

## 使用

**双击 `run.bat`** 打开图形界面（用 `pythonw` 启动，不弹黑框）。

或者命令行：

```bash
python gui.py                    # 图形界面：选文件夹 → 开始转换
python gui.py "D:\图片目录"       # 命令行直接转换（不需要 tkinter）
python gui.py "D:\图片目录" --highlight "59,69,179,199" --out "D:\结果.xlsx"
python gui.py --selftest         # 自检（纯函数断言 + 跑一遍上级目录并核对）
```

命令行输出默认落在图片目录下，命名为 `账单明细_<起始年>-<结束年>.xlsx`。
**跑之前请确认图片目录里没有无关的临时截图**——目录下所有 png/jpg 都会参与识别。

## 输出结构

| 工作表 | 内容 |
|---|---|
| `汇总` | 每个账单期间的笔数 / 支出 / 收入 / 与页面汇总卡的差异 + 文件对照表 |
| `全部明细(去重)` | 跨段、跨图去重后的全部交易，带「来源工作表」列 |
| `<年份>年`、`<年份>年-第N段` | 每张唯一截图一个表，按原图内容 |

黄色底纹 = 金额命中 `--highlight` 的交易（默认 59 / 69 / 179 / 199 元）。

## 模块

| 文件 | 职责 |
|---|---|
| `ocr.py` | 扫描目录、按内容 MD5 去重、分块 OCR → 文字块坐标 |
| `parse.py` | 文字块 → 交易记录；汇总卡提取；同期间多段合并去重；勾稽校验（仅标准库，可直接单测） |
| `excel.py` | 导出 Excel 与高亮样式 |
| `gui.py` | 入口：tkinter 界面 / 命令行 / `--selftest` |
| `build_exe.py` | 打包脚本（PyInstaller 封装） |
| `run.bat` | 双击启动图形界面 |
| `app.ico` / `icon.png` | exe 图标及其源图 |

## 打包成 exe

必须用带 tkinter 的那个环境来打包（否则 exe 里没有 tcl/tk）；PyInstaller 也要装在那个环境里：

```bash
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\pip install pyinstaller
cd /d D:\ucredit\liyuting\Desktop\转表格\bill2excel
D:\ucredit\liyuting\.workbuddy\binaries\python\envs\tk311\Scripts\python.exe build_exe.py
```

`build_exe.py` 用 Python 调 PyInstaller（而不是直接敲命令行），中文 exe 名不会因 shell 编码变乱码。
默认 `--onedir`，产物在 `dist\账单转表格\`；加 `--onefile` 出单文件版。

要点：
- 必须 `--collect-all rapidocr_onnxruntime`（30MB 的 OCR 模型在包里）和 `--collect-all onnxruntime`（原生 DLL）。
- `openssl`/`opencv` 占体积大头（cv2 单个 112MB），所以成品约 300MB，这是 OCR 的固有成本。
- 程序对 tkinter 是硬依赖，`gui.py` 里对 `ocr`/`excel` 是延迟导入，所以开窗不会被模型加载拖慢。
- 目标机器需要 MSVC 运行时（Win10+ 一般自带）。

## 已知限制

- 只适配这一种账单长截图的版式（交易类型在左、金额右对齐、卡号+时间与余额在下一行）。
  换 App 需要改 `parse.py` 里的正则与坐标阈值。
- OCR 单线程跑，一张 4 万像素高的长截图约 40 秒；整批几十张需要几分钟，属正常。
- 长截图拼接时可能漏抓个别交易：一旦明细合计与页面汇总卡对不上，汇总表会标红并给出差额。
