# -*- mode: python ; coding: utf-8 -*-
# MultiDL 打包配置
#
# 内置内容（用户无需装 Python / 依赖）：
#   - curl_cffi       反反爬兜底（Chrome 指纹）
#   - selenium        「浏览器指纹模式」驱动真实 Chrome
#   - chromedriver    随包分发（drivers/chromedriver.exe），免联网下载
#
# playwright 不打包：它需要额外下载 ~180MB 的 Chromium，且功能与 Selenium 重复。
# 源码运行时装了 playwright 会自动作为后备后端。

from PyInstaller.utils.hooks import collect_all

datas = [('drivers/chromedriver.exe', 'drivers')]
binaries = []
hiddenimports = []

for pkg in ('curl_cffi', 'selenium', 'webdriver_manager'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:                      # noqa: BLE001
        print(f"[spec] collect_all({pkg}) 跳过：{e}")

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['playwright', 'pytest', 'numpy', 'pandas', 'matplotlib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MultiDL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['chromedriver.exe'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
