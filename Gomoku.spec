# -*- mode: python ; coding: utf-8 -*-

import os

# 打包脚本（build.py）会把固化的服务地址写入 build/build_info.json，
# 这里在文件存在时把它一起打进 exe，运行时由 gomoku.client.build_info 读取。
# SPECPATH 是 PyInstaller 提供的 spec 文件所在目录（绝对路径），
# 因此不依赖当前工作目录。
_BUILD_INFO = os.path.join(SPECPATH, 'build', 'build_info.json')

datas = [
    ('gomoku/client/assets', 'gomoku/client/assets'),
]
if os.path.isfile(_BUILD_INFO):
    datas.append((_BUILD_INFO, '.'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='Gomoku',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
