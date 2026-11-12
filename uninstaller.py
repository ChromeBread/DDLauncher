"""DDLauncher の安全アンインストーラー（DDLauncher-Uninstall.exe）。

1. 設定をエクスポート  2. 削除するものを画面で確認  3. 選んだものだけをゴミ箱へ移す。
消してよいのは次のものだけ（それ以外は一覧に出さず、消す直前にも is_allowed で確かめる）:
  - %APPDATA%\\CBApps\\DDLauncher と、旧バージョンの %APPDATA%\\DDLauncher
  - 名前が DDLauncher*.exe のファイル（今の版・旧版・開発中のビルド）
  - Windows の自動起動の登録（HKCU\\...\\Run の DDLauncher）
"""
import json
import os
import re
import sys

from PySide6.QtCore import QLockFile, Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

import config
import i18n
import theme
import winproc
from i18n import tr
from settings_dialog import confirm, message

EXE_RE = re.compile(r"^DDLauncher.*\.exe$", re.IGNORECASE)
FOLDERS = (config.CONFIG_DIR, config.LEGACY_DIR)
SELF = os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else ""
SHOW_MAX = 30   # 中身の一覧はこの件数まで（大量のファイルで固まらないように）


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_allowed(path: str) -> bool:
    """ソフトと関係ないものを消さないための最後の確認。"""
    if any(_same(path, d) for d in FOLDERS):
        return True
    return os.path.isfile(path) and bool(EXE_RE.match(os.path.basename(path))) and not (SELF and _same(path, SELF))


def find_exes(folder: str) -> list[str]:
    """フォルダの直下だけを見る（下の階層や別の場所は探さない）。"""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [p for p in (os.path.join(folder, n) for n in names) if is_allowed(p) and os.path.isfile(p)]


def default_exe_folders() -> list[str]:
    if SELF:
        return [os.path.dirname(SELF)]
    here = os.path.dirname(os.path.abspath(__file__))
    return [here, os.path.join(here, "dist")]


def read_config() -> dict:
    """エクスポート用。config.load() と違い、旧フォルダからのコピーやフォルダ作成はしない。"""
    for path in (config.CONFIG_PATH, os.path.join(config.LEGACY_DIR, "config.json")):
        try:
            with open(path, encoding="utf-8") as f:
                return config.normalize(json.load(f))
        except (OSError, ValueError):
            continue
    return config.normalize({})


def ddlauncher_running() -> bool:
    for d in FOLDERS:
        if os.path.isdir(d):
            lock = QLockFile(os.path.join(d, "ddlauncher.lock"))
            if not lock.tryLock(100):
                return True
            lock.unlock()   # shortcut: 確認してから削除までの間に起動されると防げない。問題になれば削除中もロックを持つ
    return False


class Uninstaller(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("un.title"))
        self.resize(640, 560)
        self.exe_folders = default_exe_folders()
        lay = QVBoxLayout(self)

        g1 = QGroupBox(tr("un.step1"))
        v1 = QVBoxLayout(g1)
        v1.addWidget(QLabel(tr("un.step1.hint"), wordWrap=True))
        b = QPushButton(tr("un.export"))
        b.clicked.connect(self._export)
        v1.addWidget(b, alignment=Qt.AlignLeft)
        lay.addWidget(g1)

        g2 = QGroupBox(tr("un.step2"))
        v2 = QVBoxLayout(g2)
        v2.addWidget(QLabel(tr("un.step2.hint"), wordWrap=True))
        self.tree = QTreeWidget(headerHidden=True)
        v2.addWidget(self.tree, 1)
        b = QPushButton(tr("un.add_folder"))
        b.clicked.connect(self._add_folder)
        v2.addWidget(b, alignment=Qt.AlignLeft)
        lay.addWidget(g2, 1)

        row = QHBoxLayout()
        row.addStretch()
        run = QPushButton(tr("un.run"))
        run.clicked.connect(self._run)
        close = QPushButton(tr("un.close"))
        close.clicked.connect(self.reject)
        row.addWidget(run)
        row.addWidget(close)
        lay.addLayout(row)
        self._refresh()

    # ---- 一覧 ----
    def _item(self, label: str, kind: str, path: str = "") -> QTreeWidgetItem:
        it = QTreeWidgetItem([label])
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(0, Qt.Checked)
        it.setData(0, Qt.UserRole, (kind, path))
        self.tree.addTopLevelItem(it)
        return it

    def _refresh(self) -> None:
        self.tree.clear()
        for key, d in (("un.item.config", config.CONFIG_DIR), ("un.item.legacy", config.LEGACY_DIR)):
            if os.path.isdir(d):
                it = self._item(tr(key, path=d), "path", d)
                try:
                    names = sorted(os.listdir(d))
                except OSError:
                    names = []
                for n in names[:SHOW_MAX]:
                    it.addChild(QTreeWidgetItem([n]))
                if len(names) > SHOW_MAX:
                    it.addChild(QTreeWidgetItem([tr("un.more", n=len(names) - SHOW_MAX)]))
        if winproc.is_startup_enabled():
            self._item(tr("un.item.startup"), "startup")
        seen = set()
        for folder in self.exe_folders:
            for p in find_exes(folder):
                if os.path.normcase(p) not in seen:
                    seen.add(os.path.normcase(p))
                    self._item(tr("un.item.exe", path=p), "path", p)
        if not self.tree.topLevelItemCount():
            self.tree.addTopLevelItem(QTreeWidgetItem([tr("un.nothing")]))

    def _checked(self) -> list[QTreeWidgetItem]:
        items = (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
        return [it for it in items if it.data(0, Qt.UserRole) and it.checkState(0) == Qt.Checked]

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("un.add_folder"))
        if not folder:
            return
        if not find_exes(folder):
            message(self, tr("un.title"), tr("un.none_found"))
            return
        self.exe_folders.append(folder)
        self._refresh()

    # ---- 1. エクスポート ----
    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("dbg.export_title"), "DDLauncher-settings.json",
                                              tr("dbg.json_filter"))   # 上書きするときは Windows が確認を出す
        if not path:
            return
        try:
            config.write_json(path, config.export_data(read_config(), list(config.SECTIONS)))
        except OSError as e:
            message(self, tr("dbg.export_title"), tr("dbg.failed", e=e), warn=True)
            return
        message(self, tr("dbg.export_title"), tr("dbg.exported", path=path))

    # ---- 3. 削除 ----
    def _run(self) -> None:
        items = self._checked()
        if not items:
            message(self, tr("un.title"), tr("un.select_none"))
            return
        if ddlauncher_running():
            message(self, tr("un.title"), tr("un.running"), warn=True)
            return
        names = "\n".join("- " + it.text(0) for it in items)
        if not confirm(self, tr("un.title"), tr("un.confirm", items=names)):
            return
        failed = []
        for it in items:
            kind, path = it.data(0, Qt.UserRole)
            if kind == "startup":
                try:
                    winproc.set_startup(False)
                except OSError:
                    failed.append(it.text(0))
            elif not (is_allowed(path) and config.move_to_trash(path)):
                failed.append(it.text(0))
        parent = os.path.dirname(config.CONFIG_DIR)   # CBApps は他のソフトも使う。空になったときだけ片付ける
        if os.path.isdir(parent) and not os.listdir(parent):
            config.move_to_trash(parent)
        text = tr("un.failed", items="\n".join(failed)) if failed else tr("un.done")
        if SELF:
            text += "\n\n" + tr("un.self", path=SELF)
        message(self, tr("un.title"), text, warn=bool(failed))
        self._refresh()


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(theme.app_icon())
    i18n.set_language(read_config()["language"])
    Uninstaller().exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
