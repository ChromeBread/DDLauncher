# -*- mode: python ; coding: utf-8 -*-
# 1 フォルダ版: dist\DDLauncher\ に DDLauncher.exe / DDLauncher-Uninstall.exe / _internal\ を作る。
# スタンドアロン版はこのフォルダを zip にしたもの、インストーラー版は installer\DDLauncher.iss がこのフォルダを入れる。
import datetime
import json

# ビルド日を exe に入れる（情報タブに表示）
with open('build_info.json', 'w', encoding='utf-8') as f:
    json.dump({'build_date': datetime.date.today().isoformat()}, f)

EXCLUDES = ['PySide6.QtWebEngineCore', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.Qt3DCore', 'PySide6.QtMultimedia']

a = Analysis(
    ['main.py'],
    datas=[('locales', 'locales'), ('build_info.json', '.')],   # 言語ファイル・ヘルプ・ビルド日
    excludes=EXCLUDES,
)
exe = EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name='DDLauncher', upx=False, console=False)

# 安全アンインストーラー（DDLauncher-Uninstall.exe）。本体と同じフォルダ・同じ _internal を使う
ua = Analysis(
    ['uninstaller.py'],
    datas=[('locales', 'locales')],
    excludes=EXCLUDES,
)
uexe = EXE(PYZ(ua.pure), ua.scripts, [], exclude_binaries=True, name='DDLauncher-Uninstall', upx=False, console=False)

COLLECT(exe, a.binaries, a.datas, uexe, ua.binaries, ua.datas, name='DDLauncher', upx=False)
