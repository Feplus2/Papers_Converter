# -*- mode: python ; coding: utf-8 -*-
# Papers_Converter headless CLI — SageRead sidecar 打包配置
# 构建: .venv\Scripts\pyinstaller papers_converter_cli.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['pipeline.py'],
    pathex=[],
    binaries=[],
    # pypinyin 的拼音词典数据（pinyin_dict.json / phrases_dict.json）
    datas=collect_data_files('pypinyin'),
    hiddenimports=['mineru', 'fitz', 'pymupdf', 'openai', 'yaml', 'pypinyin', 'requests',
                   # ocr_provider 用 importlib 懒加载 provider，静态分析扫不到，必须显式列出
                   'ocr_provider', 'stage1_mineru', 'stage1_glm', 'stage1_paddleocr',
                   'stage1_layout'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'torch', 'transformers', 'accelerate'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='papers_converter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,   # headless CLI 需要 stdout 管道；Tauri sidecar 会隐藏窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
