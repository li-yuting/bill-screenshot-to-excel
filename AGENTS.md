# Repository Guidelines

`billtools` is a Windows-focused Python 3.11+ tool that converts bill screenshots to Excel using local OCR. Keep screenshots and transaction data local; never commit them.

## Project Structure & Module Organization

- `billtools/app.py`: tkinter console, CLI, and self-test entry point.
- `billtools/ocr.py`: image discovery, MD5 deduplication, OCR, red-box detection.
- `billtools/statement.py`: long-statement parsing, self-calibration, grouping, reconciliation.
- `billtools/detail.py`: single-transaction parser; add layouts to `PROFILES`.
- `billtools/excel.py`: workbook styles and exports.
- `billtools/build_exe.py`, `smoke_test.py`, `run.bat`: packaging, packaged-app checks, Windows launcher.
- `docs/superpowers/specs/`: design decisions; read the relevant spec before parser changes.
- `samples/` (when present): fully redacted reference images only.

## Build, Test, and Development Commands

Use Python 3.11+ with tkinter and install `rapidocr-onnxruntime`, `openpyxl`, and `pillow`.

```powershell
python billtools/app.py
python billtools/app.py statement <image-dir>
python billtools/app.py detail <image-dir>
python billtools/statement.py
python billtools/detail.py
python billtools/ocr.py
python billtools/build_exe.py
python billtools/smoke_test.py
```

The `app.py` commands open the GUI or run a conversion; module scripts run image-free selftests. `build_exe.py` creates `billtools/dist/账单截图转Excel.exe`. Run `smoke_test.py` afterward because a zero build exit code does not guarantee the executable exists.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case`, `PascalCase`, and `UPPER_SNAKE_CASE` for constants and regexes. Prefer type hints and short docstrings. Keep `statement.py` and `detail.py` standard-library-only; preserve the Windows and Chinese UI assumptions. No formatter or linter is configured: match nearby code and run `python -m compileall billtools`.

## Testing Guidelines

There is no pytest suite or coverage threshold. Add assertions to the relevant `selftest()` and run all three module selftests. Parser changes need a regression case and an end-to-end check such as `python billtools/app.py --selftest detail <image-dir>` or `python billtools/app.py --selftest statement <image-dir>`. Use synthetic or redacted images only. After packaging, `smoke_test.py` must verify self-test output and a real GUI window.

## Commit & Pull Request Guidelines

History uses concise Chinese subject lines naming the component and outcome; keep one focused change per commit. PRs should explain behavior, list test commands and results, link issues, include UI screenshots, and note packaging or smoke-test results when relevant.

## Security & Configuration

Never commit `账单*/`, `outputs/`, `*.xlsx`, `*.xls`, `.workbuddy/`, OCR text, or real transaction data. Keep these paths in `.gitignore` and redact screenshots before adding them. Generated `build/`, `dist/`, and `.spec` files stay out of commits.

## Architecture Overview

The flow is `ocr.py` -> `statement.py` or `detail.py` -> `excel.py`; `app.py` coordinates GUI and CLI work. Parser code is deterministic and independent of GUI state.
