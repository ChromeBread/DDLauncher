"""D&D で追加するファイルの名前が OBS のソース / シーン / グループと被ったとき、どうするかを選ぶ画面。"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from i18n import tr

ACTIONS = ["number", "rename", "reuse", "skip"]   # 連番を付ける / 名前を変える / 既存のソースを配置 / 追加しない


def plan_problems(rows: list[dict], fixed: set[str], taken: set[str]) -> list[str]:
    """選んだ内容の問題（空の名前、使用済みの名前、同じ名前が 2 つ）。rows: {"action", "name"}。fixed = 被らないファイルの名前。"""
    out, seen = [], set(fixed)
    for r in rows:
        if r["action"] not in ("number", "rename"):
            continue
        name = r["name"].strip()
        if not name:
            out.append(tr("conflict.err.empty"))
        elif name in taken:
            out.append(tr("conflict.err.taken", name=name))
        elif name in seen:
            out.append(tr("conflict.err.dup", name=name))
        seen.add(name)
    return list(dict.fromkeys(out))   # 同じ文は 1 回だけ


class ConflictDialog(QDialog):
    """conflicts: [(パス, 元の名前, 連番の案)]。inputs: OBS のソース名（既存のソースを配置できるのはこれだけ）。"""

    def __init__(self, conflicts: list[tuple[str, str, str]], fixed: set[str], inputs: set[str], taken: set[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("conflict.title"))
        self.resize(640, 360)
        self.conflicts, self.fixed, self.taken = conflicts, fixed, taken
        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr("conflict.body"), wordWrap=True))
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("conflict.all")))
        self.all = QComboBox()
        for a in ACTIONS:
            self.all.addItem(tr(f"conflict.act.{a}"), a)
        self.all.activated.connect(lambda _: self._set_all(self.all.currentData()))
        top.addWidget(self.all)
        top.addStretch()
        v.addLayout(top)

        t = self.table = QTableWidget(len(conflicts), 3)
        t.setHorizontalHeaderLabels([tr("conflict.col.file"), tr("conflict.col.action"), tr("conflict.col.name")])
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        t.horizontalHeader().setStretchLastSection(True)
        t.setColumnWidth(0, 200)
        t.setColumnWidth(1, 200)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        self.combos, self.edits = [], []
        for r, (path, base, suggestion) in enumerate(conflicts):
            item = QTableWidgetItem(os.path.basename(path))   # 配信に映ってもフォルダ構造が見えないよう名前だけ
            item.setFlags(Qt.ItemIsEnabled)
            t.setItem(r, 0, item)
            combo = QComboBox()
            for a in ACTIONS:
                if a != "reuse" or base in inputs:   # シーンやグループと被った場合は使い回せない
                    combo.addItem(tr(f"conflict.act.{a}"), a)
            edit = QLineEdit(suggestion)
            combo.currentIndexChanged.connect(lambda _, row=r: self._row_changed(row))
            edit.textChanged.connect(self._check)
            t.setCellWidget(r, 1, combo)
            t.setCellWidget(r, 2, edit)
            self.combos.append(combo)
            self.edits.append(edit)
            self._row_changed(r)
        v.addWidget(t, 1)
        self.error = QLabel(wordWrap=True)
        self.error.setStyleSheet("color: #d33c3c;")
        v.addWidget(self.error)
        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("dnd.pick.add"), QDialogButtonBox.AcceptRole)
        bb.addButton(tr("btn.cancel"), QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._check()

    def _set_all(self, action: str) -> None:
        for combo in self.combos:
            i = combo.findData(action)
            if i >= 0:   # 「既存のソースを配置」が選べない行はそのまま
                combo.setCurrentIndex(i)

    def _row_changed(self, r: int) -> None:
        action = self.combos[r].currentData()
        path, base, suggestion = self.conflicts[r]
        edit = self.edits[r]
        edit.blockSignals(True)
        if action == "number":
            edit.setText(suggestion)
        elif action == "reuse":
            edit.setText(base)
        elif action == "skip":
            edit.setText("")
        edit.blockSignals(False)
        edit.setReadOnly(action != "rename")   # 名前を入力できるのは「名前を変える」のときだけ
        edit.setEnabled(action != "skip")
        if action == "rename":
            edit.setFocus()
            edit.selectAll()
        self._check()

    def rows(self) -> list[dict]:
        return [{"action": c.currentData(), "name": e.text()} for c, e in zip(self.combos, self.edits)]

    def _check(self) -> None:
        if not hasattr(self, "ok"):
            return
        problems = plan_problems(self.rows(), self.fixed, self.taken)
        self.error.setText("\n".join(problems))
        self.ok.setEnabled(not problems)

    def plan(self) -> dict:
        """{パス: ("new", 名前) / ("reuse", 既存のソース名) / ("skip", None)}"""
        out = {}
        for (path, base, _), row in zip(self.conflicts, self.rows()):
            if row["action"] == "skip":
                out[path] = ("skip", None)
            elif row["action"] == "reuse":
                out[path] = ("reuse", base)
            else:
                out[path] = ("new", row["name"].strip())
        return out
