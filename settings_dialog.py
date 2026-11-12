"""設定ダイアログ。ランチャー 1 つ分のアプリ一覧 + ポート管理 + 全体設定を編集する。"""
import copy
import json
import os
import re
import unicodedata

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QGroupBox, QInputDialog, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

import about
import config
from launcher_window import load_app_icon
import i18n
from obs_bridge import unique_name
import sequencer
import sound
import theme
import winproc

tr = i18n.tr

# (キー, 既定の幅)。見出しは col.<キー>、説明は col.<キー>.tip（言語ファイル）
COLS = [("name", 110), ("group", 80), ("path", 260), ("icon", 160), ("args", 120), ("delay_sec", 110),
        ("minimized", 80), ("admin", 80), ("autostart", 110), ("obs_guard", 130), ("monitor", 170), ("process_name", 110)]
BOOL_KEYS = {"minimized", "admin", "autostart", "obs_guard"}
MONITOR_COL = [c[0] for c in COLS].index("monitor")
PATH_COL = [c[0] for c in COLS].index("path")
ICON_COL = [c[0] for c in COLS].index("icon")
DELAY_COL = [c[0] for c in COLS].index("delay_sec")
PORT_COLS = [("port", 70), ("memo", 180), ("process", 130), ("status", 330)]   # 見出しは pcol.<キー>
PORT_COLORS = {"free": "#808080", "used": "#2a9d6a", "conflict": "#d33c3c", "moved": "#d08a1a"}
OTHER = "other"   # 未登録の使用中ポート行の目印


def _setup_columns(table: QTableWidget, cols, widths: list[int], prefix: str, tips: bool) -> None:
    """全列をドラッグで幅変更可能に。保存済みの幅があれば復元。"""
    table.setHorizontalHeaderLabels([tr(f"{prefix}.{key}") for key, _ in cols])
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(False)
    use_saved = len(widths) == len(cols)
    for i, (key, width) in enumerate(cols):
        table.setColumnWidth(i, widths[i] if use_saved else width)
        if tips:
            table.horizontalHeaderItem(i).setToolTip(tr(f"{prefix}.{key}.tip"))


def _col_widths(table: QTableWidget) -> list[int]:
    return [table.columnWidth(i) for i in range(table.columnCount())]


class WrapCheck(QWidget):
    """折り返すチェックボックス。QCheckBox の文字は折り返さず、訳が長い言語で設定画面が横にはみ出すため。"""

    def __init__(self, text: str, checked: bool = False):
        super().__init__()
        self.box = QCheckBox(checked=checked)
        self.label = QLabel(text, wordWrap=True)
        self.label.mousePressEvent = lambda e: self.box.toggle()   # 文字をクリックしても切り替わる
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.box, 0, Qt.AlignTop)
        lay.addWidget(self.label, 1)

    def isChecked(self) -> bool:
        return self.box.isChecked()

    def setChecked(self, on: bool) -> None:
        self.box.setChecked(on)


def message(parent, title: str, text: str, warn: bool = False) -> None:
    """QMessageBox.information / warning の代わり。標準の「OK」は Qt の英語のままなので訳を入れる。"""
    box = QMessageBox(QMessageBox.Warning if warn else QMessageBox.Information, title, text, QMessageBox.Ok, parent)
    box.button(QMessageBox.Ok).setText(tr("btn.ok"))
    box.exec()


def confirm(parent, title: str, text: str) -> bool:
    """はい / いいえ の確認（既定は「いいえ」= 誤操作で実行しない）。"""
    box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Yes | QMessageBox.No, parent)
    box.button(QMessageBox.Yes).setText(tr("btn.yes"))
    box.button(QMessageBox.No).setText(tr("btn.no"))
    box.setDefaultButton(QMessageBox.No)
    return box.exec() == QMessageBox.Yes


def clean_seconds(text: str) -> str:
    """待ち時間の入力を数字だけにする。全角は半角に、「秒」「s」などの文字は消す。数字が無ければ 0。"""
    m = re.search(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", text))
    return m.group(0) if m else "0"


def fmt_sec(sec: float) -> str:
    """秒数を言語ごとの単位で。60 秒以上は「1分35秒」の形にする（「95s」より頭に入りやすい）。"""
    sec = round(sec, 1)
    if sec < 60:
        return tr("seq.sec", s=f"{sec:g}")
    m, s = int(sec // 60), round(sec % 60, 1)
    return tr("seq.min", m=m) if s == 0 else tr("seq.min_sec", m=m, s=f"{s:g}")


def _hint(text: str) -> QLabel:
    lb = QLabel(text, wordWrap=True)
    lb.setEnabled(False)   # 灰色の補足文
    return lb


class DragRowTable(QTableWidget):
    """行をドラッグで並べ替えられるテーブル。

    QTableWidget 標準の InternalMove は移動先の行を上書きしてしまうため、
    ドロップ位置だけ受け取って移動は呼び出し側（row_moved）で行う。
    """
    row_moved = Signal(int, int)   # (移動元, 挿入先 index)

    def __init__(self, rows: int, cols: int):
        super().__init__(rows, cols)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def dropEvent(self, e):
        src = self.currentRow()
        idx = self.indexAt(e.position().toPoint())
        if not idx.isValid():
            dst = self.rowCount()
        else:
            dst = idx.row() + (1 if self.dropIndicatorPosition() == QAbstractItemView.BelowItem else 0)
        e.setDropAction(Qt.IgnoreAction)   # 標準の「移動元を削除」を走らせない
        e.accept()
        if src >= 0:
            self.row_moved.emit(src, dst)


class SequenceTimeline(QWidget):
    """起動シーケンスの時間グラフ。1 行 = 起動するアプリ、横軸 = 開始からの秒数。待ち時間は前の印からの線で見せる。"""
    ROW, NAME_W, PAD = 20, 140, 8

    def __init__(self):
        super().__init__()
        self.items: list[tuple[str, float, float]] = []   # (名前, 待ち秒, 開始からの秒)

    def set_apps(self, apps: list[dict]) -> None:
        self.items = [(apps[i].get("name") or apps[i].get("path", ""), d, t) for i, d, t in sequencer.timeline(apps)]
        self.setMinimumHeight(self.ROW * max(len(self.items), 1) + self.ROW + self.PAD)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        pal = self.palette()
        fm = p.fontMetrics()
        if not self.items:
            p.setPen(pal.color(QPalette.PlaceholderText))
            p.drawText(self.rect(), Qt.AlignCenter, tr("seq.empty"))
            return
        total = self.items[-1][2]
        label = lambda d, t: fmt_sec(t) + ("  " + tr("seq.wait", t=fmt_sec(d)) if d else "")
        x0 = self.NAME_W + self.PAD
        x1 = self.width() - self.PAD - 9 - fm.horizontalAdvance(label(*self.items[-1][1:]))   # 最後の印の文字が切れないように
        sx = lambda t: x0 + (x1 - x0) * (t / total if total > 0 else 0)
        axis_y = self.ROW * len(self.items) + self.PAD
        p.setPen(pal.color(QPalette.Mid))
        p.drawLine(x0, axis_y, x1, axis_y)
        # 目盛りは 10 本まで、かつ「1分10秒」のような長い文字が重ならない間隔にする
        step = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 10 ** 9)
                    if total / s <= 10 and (x1 - x0) * s / max(total, 1e-9) >= fm.horizontalAdvance(fmt_sec(total + s)) + 12)
        for k in range(0, int(total // step) + 1):
            x = round(sx(k * step))
            p.drawLine(x, axis_y, x, axis_y + 4)
            p.drawText(x - 40, axis_y + 4, 80, self.ROW - 4, Qt.AlignHCenter | Qt.AlignTop, fmt_sec(k * step))
        prev = 0.0
        for row, (name, delay, t) in enumerate(self.items):
            y = self.PAD + row * self.ROW + self.ROW // 2
            p.setPen(pal.color(QPalette.Text))
            p.drawText(0, y - self.ROW // 2, self.NAME_W, self.ROW, Qt.AlignVCenter | Qt.AlignRight,
                       fm.elidedText(name, Qt.ElideRight, self.NAME_W))
            p.setPen(QPen(pal.color(QPalette.Mid), 2, Qt.DotLine))
            p.drawLine(round(sx(prev)), y, round(sx(t)), y)   # 前のアプリからの待ち時間
            p.setPen(Qt.NoPen)
            p.setBrush(pal.color(QPalette.Highlight))
            p.drawEllipse(QPoint(round(sx(t)), y), 5, 5)
            p.setPen(pal.color(QPalette.Text))
            p.drawText(round(sx(t)) + 9, y - self.ROW // 2, 300, self.ROW, Qt.AlignVCenter,
                       label(delay, t))
            prev = t


class SettingsDialog(QDialog):
    sound_apply = Signal(object, bool)   # サウンドプロファイルの「適用」: (プロファイル, 音量とミュートも戻すか)
    colors_preview = Signal(object, int)   # カラーエディターで色 / 不透明度が変わった: (custom の色 dict / 標準なら None, 不透明度 %)

    def __init__(self, launcher_index: int, cfg: dict, parent=None):
        super().__init__(parent)
        self._preview_timer = QTimer(self, singleShot=True, interval=30)   # 色選択のドラッグ中に描き直しすぎない
        self._preview_timer.timeout.connect(
            lambda: self.colors_preview.emit(dict(self.colors) if self.color_mode.currentData() == "custom" else None,
                                             self.opacity.value()))
        self.setWindowTitle(tr("set.title"))
        self.resize(1000, 620)   # 起動アプリの表と時間グラフが両方見える高さ
        self.cfg = copy.deepcopy(cfg)
        self.lcfg = self.cfg["launchers"][launcher_index]
        self.monitors = winproc.monitors()
        self.listening: dict = {}
        self.imported = None   # インポートしたら、呼び出し側がこの設定で作り直す

        tabs = self.tabs = QTabWidget()
        tabs.addTab(self._apps_tab(), tr("set.tab.apps"))
        tabs.addTab(self._ports_tab(), tr("set.tab.ports"))
        tabs.addTab(self._general_tab(), tr("set.tab.general"))
        tabs.addTab(self._color_tab(), tr("set.tab.colors"))
        self.sound_tab_index = tabs.addTab(self._sound_tab(), tr("set.tab.sound"))   # Debug Mode ON のときだけ見せる
        tabs.addTab(self._info_tab(), tr("set.tab.info"))
        tabs.currentChanged.connect(lambda _: self._fill_mini_icon())   # 起動アプリ タブで足したアプリも選べるように
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr("btn.ok"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("btn.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        QApplication.instance().installEventFilter(self)   # 後から足す行のプルダウンにも効かせるため app 全体で見る
        for w in self.findChildren(QAbstractSpinBox) + self.findChildren(QComboBox):   # ここまでに作った欄（後から足す欄は Polish で）
            w.setFocusPolicy(Qt.StrongFocus)

    def eventFilter(self, obj, e):
        """クリックしていない数値欄 / プルダウンはホイールで値を変えず、周りのスクロールに回す。"""
        if e.type() == QEvent.Polish and isinstance(obj, (QAbstractSpinBox, QComboBox)) and obj.window() is self:
            obj.setFocusPolicy(Qt.StrongFocus)   # 既定の WheelFocus だとホイールが当たるだけでフォーカスが入り、下の判定をすり抜ける
        if (e.type() == QEvent.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox))
                and not obj.hasFocus() and obj.window() is self):
            w = obj.parentWidget()
            while w is not None and not isinstance(w, QAbstractScrollArea):
                w = w.parentWidget()
            if w is not None:
                QApplication.sendEvent(w.viewport(), e)
            return True
        return super().eventFilter(obj, e)

    def done(self, r):
        self._preview_timer.stop()   # 閉じた後に色が届いて、キャンセルの「元に戻す」を上書きしないように
        QApplication.instance().removeEventFilter(self)
        super().done(r)

    # ================= 起動アプリ =================
    def _apps_tab(self) -> QWidget:
        w = QWidget()
        self.table = DragRowTable(0, len(COLS))
        _setup_columns(self.table, COLS, self.cfg["ui"]["app_col_widths"], "col", tips=True)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)   # 列を見出しのドラッグで入れ替え（並びは記憶する）
        order = self.cfg["ui"]["app_col_order"]
        if sorted(order) == list(range(len(COLS))):
            for visual, logical in enumerate(order):
                header.moveSection(header.visualIndex(logical), visual)
        self.table.verticalHeader().setMaximumWidth(180)   # 行見出しに表示名（横にスクロールしても左端に残る）
        self.table.row_moved.connect(self._move_row)
        self.table.itemChanged.connect(self._clean_delay)
        self._removed: list[tuple[int, dict]] = []   # 削除した行（元に戻す用）
        for app in self.lcfg["apps"]:
            self._set_row(self.table.rowCount(), app, insert=True)

        btns = QHBoxLayout()
        self.undo_btn = QPushButton(tr("btn.undo"), enabled=False)
        self.undo_btn.clicked.connect(self._undo)
        QShortcut(QKeySequence.Undo, self.table, self._undo)
        for text, slot in ((tr("btn.add"), self._add), (tr("btn.remove"), self._remove), ("↑", lambda: self._move(-1)),
                           ("↓", lambda: self._move(1)), (tr("btn.browse_app"), self._browse),
                           (tr("btn.browse_icon"), self._browse_icon)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
            if slot == self._remove:
                btns.addWidget(self.undo_btn)
        btns.addStretch()
        self.timeline = SequenceTimeline()
        tl_box = QGroupBox(tr("seq.timeline"))
        tl_scroll = self._tl_scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.NoFrame, maximumHeight=220)   # アプリが多くても表を潰さない
        tl_scroll.setWidget(self.timeline)
        QVBoxLayout(tl_box).addWidget(tl_scroll)
        self._timeline_timer = QTimer(self, singleShot=True, interval=0)   # 1 行足すとセルの数だけ変更が来るので 1 回にまとめる
        self._timeline_timer.timeout.connect(self._update_timeline)
        self.table.itemChanged.connect(self._timeline_timer.start)
        for sig in (self.table.model().rowsInserted, self.table.model().rowsRemoved, self.table.model().rowsMoved):
            sig.connect(self._timeline_timer.start)
        self._timeline_timer.start()
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.table, 1)
        tl.addLayout(btns)
        tl.addWidget(_hint(tr("apps.hint")))
        self.app_split = QSplitter(Qt.Vertical, childrenCollapsible=False)   # 表と時間グラフの境目をドラッグで変えられる（記憶する）
        self.app_split.addWidget(top)
        self.app_split.addWidget(tl_box)
        if len(self.cfg["ui"]["app_split"]) == 2:
            self.app_split.setSizes(self.cfg["ui"]["app_split"])
        lay = QVBoxLayout(w)
        lay.addWidget(self.app_split)
        return w

    def _update_timeline(self) -> None:
        apps = [self._row_app(r) for r in range(self.table.rowCount())]
        self.timeline.set_apps(apps)
        self.table.setVerticalHeaderLabels([f"{r + 1}  {a['name'][:16]}" for r, a in enumerate(apps)])
        self._tl_scroll.setMinimumHeight(min(self.timeline.minimumHeight() + 4, 220))   # 220px までは全部見せ、それ以上はスクロール

    def _monitor_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        cb.addItem(tr("monitor.none"), "")
        for i, m in enumerate(self.monitors, 1):
            l, t, r, b = m["rect"]
            label = f"{i}: {m['name'].removeprefix(chr(92) * 2 + '.' + chr(92))} {r - l}x{b - t}"   # \\.\DISPLAY1 → DISPLAY1
            cb.addItem(label + (tr("monitor.primary") if m["primary"] else ""), m["name"])
        if current and cb.findData(current) < 0:   # 今は繋がっていないモニタの設定も消さない
            cb.addItem(tr("monitor.disconnected", name=current), current)
        cb.setCurrentIndex(max(0, cb.findData(current)))
        return cb

    def _set_row(self, r: int, app: dict, insert=False) -> None:
        if insert:
            self.table.insertRow(r)
        for c, (key, _) in enumerate(COLS):
            item = QTableWidgetItem()
            if key in BOOL_KEYS:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
                item.setCheckState(Qt.Checked if app.get(key) else Qt.Unchecked)
            elif key != "monitor":
                item.setText(str(app.get(key, "")))
            self.table.setItem(r, c, item)
        self.table.setCellWidget(r, MONITOR_COL, self._monitor_combo(app.get("monitor", "")))
        self.table.item(r, 0).setData(Qt.UserRole, app)   # workdir など表に無いキーを保持

    def _row_app(self, r: int) -> dict:
        app = dict(self.table.item(r, 0).data(Qt.UserRole) or config.DEFAULT_APP)
        for c, (key, _) in enumerate(COLS):
            item = self.table.item(r, c)
            if key in BOOL_KEYS:
                app[key] = item.checkState() == Qt.Checked
            elif key == "monitor":
                app[key] = self.table.cellWidget(r, c).currentData() or ""
            elif key == "delay_sec":
                try:
                    app[key] = max(0.0, float(item.text()))
                except ValueError:
                    app[key] = 0.0
            else:
                app[key] = item.text().strip()
        return app

    def _move_row(self, src: int, dst: int) -> None:
        """src 行を「dst 番目の行の前」へ移動（dst = rowCount なら末尾）。"""
        if dst > src:
            dst -= 1
        if dst == src or not 0 <= dst < self.table.rowCount():
            return
        app = self._row_app(src)
        self.table.removeRow(src)
        self._set_row(dst, app, insert=True)
        self.table.selectRow(dst)

    def _add(self):
        self._set_row(self.table.rowCount(), dict(config.DEFAULT_APP, name=tr("apps.new_name")), insert=True)

    def _selected_row(self) -> int:
        """選んでいる行。選んでいなければ、選ぶよう案内して -1。"""
        r = self.table.currentRow()
        if r < 0:
            message(self, tr("set.tab.apps"), tr("apps.select_row"))
        return r

    def _remove(self):
        """確認なしで消す。消した行は覚えておき「元に戻す」（Ctrl+Z）で戻せる。"""
        r = self._selected_row()
        if r >= 0:
            self._removed.append((r, self._row_app(r)))
            self.table.removeRow(r)
            self.undo_btn.setEnabled(True)

    def _undo(self):
        if self._removed:
            r, app = self._removed.pop()
            r = min(r, self.table.rowCount())
            self._set_row(r, app, insert=True)
            self.table.selectRow(r)
            self.undo_btn.setEnabled(bool(self._removed))

    def _clean_delay(self, item) -> None:
        if item.column() == DELAY_COL:
            text = clean_seconds(item.text())
            if text != item.text():
                item.setText(text)   # もう一度 itemChanged が来るが、そのときは同じ文字なので止まる

    def _move(self, d: int):
        r = self._selected_row()
        if r >= 0:
            self._move_row(r, r + d + (1 if d > 0 else 0))

    def _browse(self):
        r = self._selected_row()
        if r < 0:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("dlg.pick_app"), "", tr("dlg.pick_app_filter"))
        if path:
            self.table.item(r, PATH_COL).setText(path.replace("/", "\\"))

    def _browse_icon(self):
        r = self._selected_row()
        if r < 0:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("dlg.pick_icon"), "", tr("dlg.pick_icon_filter"))
        if path:
            self.table.item(r, ICON_COL).setText(path.replace("/", "\\"))

    # ================= ポート管理 =================
    def _ports_tab(self) -> QWidget:
        w = QWidget()
        self.ports = QTableWidget(0, len(PORT_COLS))
        _setup_columns(self.ports, PORT_COLS, self.cfg["ui"]["port_col_widths"], "pcol", tips=False)
        self.ports.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ports.setSelectionMode(QAbstractItemView.SingleSelection)
        self.show_all = QCheckBox(tr("ports.show_all"))
        for p in self.cfg["ports"]:
            self._add_port_row(p)
        self.ports.itemChanged.connect(lambda it: it.column() in (0, 2) and self._refresh_ports())
        self.show_all.toggled.connect(self._refresh_ports)
        btns = QHBoxLayout()
        for text, slot in ((tr("btn.add"), lambda: self._add_port_row({"port": "", "memo": "", "process": ""})),
                           (tr("btn.remove"), self._remove_port), (tr("ports.detect"), self._detect_ports),
                           (tr("ports.rescan"), self._refresh_ports),
                           (tr("ports.release"), self._release_port)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        self.ip_mode = QComboBox()
        for key in config.IP_MODES:
            self.ip_mode.addItem(tr(f"ip.{key or 'unset'}"), key)
        self.ip_mode.setCurrentIndex(max(0, self.ip_mode.findData(self.cfg["ip_mode"])))
        self.ip_mode.setToolTip(tr("ip.tip"))
        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel(tr("ip.label")))
        ip_row.addWidget(self.ip_mode)
        b = QPushButton(tr("ip.detect"))
        b.clicked.connect(self._detect_ip)
        ip_row.addWidget(b)
        ip_row.addStretch()
        lay = QVBoxLayout(w)
        lay.addLayout(ip_row)
        lay.addWidget(self.ports)
        lay.addLayout(btns)
        lay.addWidget(self.show_all)   # ボタンと同じ行だと訳が長い言語で横にはみ出す
        return w

    def _add_port_row(self, p: dict) -> None:
        r = self.ports.rowCount()
        self.ports.blockSignals(True)
        self.ports.insertRow(r)
        for c, (key, _) in enumerate(PORT_COLS):
            item = QTableWidgetItem(str(p.get(key, "")))
            if key == "status":
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.ports.setItem(r, c, item)
        self.ports.blockSignals(False)
        self._refresh_ports()

    def _detect_ip(self):
        mode = winproc.detect_ip_mode()
        if mode is None:
            message(self, tr("ip.detect"), tr("ip.detect_none"))
            return
        self.ip_mode.setCurrentIndex(self.ip_mode.findData(mode))
        message(self, tr("ip.detect"), tr(f"ip.result.{mode}"))

    def _detect_ports(self):
        """登録アプリと配信ソフトのポートを探し、選んだものだけ取り込む（登録済みの番号は選べない）。"""
        self._refresh_ports()
        apps = [self._row_app(r) for r in range(self.table.rowCount())]
        have = {self._port_of(r) for r in range(self.ports.rowCount())}
        found, notes = winproc.scan_known_ports(apps, self.listening)
        if not any(int(p["port"]) not in have for p in found):
            message(self, tr("ports.detect"), "\n\n".join([tr("ports.detect_none")] + notes))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("ports.detect"))
        lst = QListWidget()
        for p in found:
            done = int(p["port"]) in have
            it = QListWidgetItem(f"{p['port']}  {p['memo']}  —  {tr('ports.src.' + p['src'])}"
                                 + (f"  {tr('ports.registered')}" if done else ""))
            it.setData(Qt.UserRole, p)
            it.setFlags(Qt.ItemIsUserCheckable | (Qt.NoItemFlags if done else Qt.ItemIsEnabled))
            it.setCheckState(Qt.Unchecked if done else Qt.Checked)
            lst.addItem(it)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("ports.add_checked"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("btn.cancel"))
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(tr("ports.pick"), wordWrap=True))
        lay.addWidget(lst)
        for n in notes:
            lay.addWidget(_hint(n))
        lay.addWidget(bb)
        dlg.resize(520, 360)
        if dlg.exec() != QDialog.Accepted:
            return
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.Checked and it.flags() & Qt.ItemIsEnabled:
                p = it.data(Qt.UserRole)
                self._add_port_row({k: p[k] for k in ("port", "memo", "process")})

    def _remove_port(self):
        r = self.ports.currentRow()
        if r >= 0 and self.ports.item(r, 0).data(Qt.UserRole) != OTHER:
            self.ports.removeRow(r)
            self._refresh_ports()

    def _port_of(self, r: int) -> int | None:
        try:
            return int(self.ports.item(r, 0).text())
        except (ValueError, AttributeError):
            return None

    def _refresh_ports(self, *_):
        """登録ポートの使用状況を再スキャンし、状態列を更新する。"""
        self.listening = winproc.listening_ports()
        self.ports.blockSignals(True)
        for r in reversed(range(self.ports.rowCount())):   # 前回の「未登録」行を消す
            if self.ports.item(r, 0).data(Qt.UserRole) == OTHER:
                self.ports.removeRow(r)
        registered = [self._port_of(r) for r in range(self.ports.rowCount())]
        for r, port in enumerate(registered):
            if port is None or not 1 <= port <= 65535:
                level, text = "conflict", tr("ports.invalid")
            else:
                exe = self.ports.item(r, 2).text()
                level, text = winproc.port_status(exe, self.listening.get(port, []), winproc.ports_of(exe, self.listening))
                if registered.count(port) > 1:
                    level, text = "conflict", tr("ports.duplicate", text=text)
            item = self.ports.item(r, 3)
            item.setText(text)
            item.setForeground(QColor(PORT_COLORS[level]))
        if self.show_all.isChecked():
            for port in sorted(set(self.listening) - set(registered)):
                r = self.ports.rowCount()
                self.ports.insertRow(r)
                for c, text in enumerate((str(port), tr("ports.unregistered"), "", winproc.port_status("", self.listening[port])[1])):
                    item = QTableWidgetItem(text)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    item.setForeground(QColor(PORT_COLORS["free"]))
                    self.ports.setItem(r, c, item)
                self.ports.item(r, 0).setData(Qt.UserRole, OTHER)
        self.ports.blockSignals(False)

    def _release_port(self):
        r = self.ports.currentRow()
        port = self._port_of(r) if r >= 0 else None
        owners = self.listening.get(port, []) if port else []
        if not owners:
            message(self, tr("ports.release_title"), tr("ports.not_in_use"))
            return
        desc = "\n".join(f"  {name} (PID {pid}, {proto})" for pid, name, proto in owners)
        if not confirm(self, tr("ports.release_title"), tr("ports.release_confirm", port=port, desc=desc)):
            return
        results = [f"{name}: {winproc.release_port(pid)[1]}" for pid, name, _ in owners]
        message(self, tr("ports.release_title"), "\n".join(results))
        self._refresh_ports()

    # ================= 全般 =================
    @staticmethod
    def _combo(prefix: str, keys: list[str], current: str) -> QComboBox:
        cb = QComboBox()
        for key in keys:
            cb.addItem(tr(f"{prefix}.{key}"), key)
        cb.setCurrentIndex(max(0, cb.findData(current)))
        return cb

    def _general_tab(self) -> QWidget:
        w = QWidget()
        self.corner = self._combo("corner", config.CORNERS, self.lcfg["corner"])
        self.screen = QSpinBox(maximum=max(0, len(QGuiApplication.screens()) - 1), value=self.lcfg["screen"])
        self.margin = QSpinBox(maximum=200, value=self.lcfg["margin"])
        self.dnd_flush = WrapCheck(tr("gen.dnd_flush.check"), self.lcfg["dnd_flush"])
        self.scale = QSpinBox(minimum=50, maximum=250, singleStep=10, suffix=" %",
                              value=round(self.lcfg["scale"] * 100))
        self.collapse = self._combo("collapse", config.COLLAPSE, self.lcfg["collapse"])
        self.collapse.setToolTip(tr("collapse.tip"))
        self.mini_icon = QComboBox()
        self._fill_mini_icon(self.lcfg["apps"], self.lcfg["mini_icon"])
        self.layout_mode = self._combo("layout", config.LAYOUTS, self.lcfg["layout"])
        self.max_cols = QSpinBox(maximum=50, value=self.lcfg["max_cols"], specialValueText=tr("gen.unlimited"))
        self.max_rows = QSpinBox(maximum=50, value=self.lcfg["max_rows"], specialValueText=tr("gen.unlimited"))
        self.max_rows.setToolTip(tr("gen.max_rows.tip"))
        self.layout_mode.model().item(config.LAYOUTS.index("linear")).setEnabled(False)   # LINEAR は調整中
        self.group_rows = QSpinBox(maximum=50, value=self.lcfg["group_max_rows"], specialValueText=tr("gen.unlimited"))
        self.group_rows.setToolTip(tr("gen.group_rows.tip"))
        self.group_cols = QSpinBox(maximum=50, value=self.lcfg["group_max_cols"], specialValueText=tr("gen.unlimited"))
        self.group_cols.setToolTip(tr("gen.group_cols.tip"))
        self.max_cols.setEnabled(False)
        self.max_rows.setEnabled(False)
        self.wheel_rotate = WrapCheck(tr("gen.wheel_rotate.check"), self.lcfg["wheel_rotate"])
        self.keep_rot = WrapCheck(tr("gen.keep_rot.check"), self.lcfg["keep_rotation"])
        self.show_seq = WrapCheck(tr("gen.show_seq.check"), self.lcfg["show_seq_button"])
        self.after_seq = self._combo("after", config.AFTER_SEQUENCE, self.lcfg["after_sequence"])
        self.after_seq.setToolTip(tr("after.tip"))
        self.anim = self._combo("anim", config.ANIMATIONS, self.cfg["animation"])
        self.startup = WrapCheck(tr("gen.startup.check"), winproc.is_startup_enabled())
        obs = self.cfg["obs"]
        self.host = QLineEdit(obs["host"])
        self.port = QSpinBox(minimum=1, maximum=65535, value=int(obs["port"]))
        self.password = QLineEdit(obs["password"], echoMode=QLineEdit.Password)
        self.scene = QLineEdit(obs["target_scene"], placeholderText=tr("gen.scene.placeholder"))
        groups = (   # 目的ごとに枠で分ける（1 列に 20 項目以上並ぶと探しにくいため）
            ("position", [("corner", self.corner), ("screen", self.screen), ("margin", self.margin),
                          ("dnd_flush", self.dnd_flush), ("scale", self.scale)]),
            ("display", [("collapse", self.collapse), ("mini_icon", self.mini_icon), ("layout", self.layout_mode),
                         ("group_rows", self.group_rows), ("group_cols", self.group_cols), ("max_cols", self.max_cols),
                         ("max_rows", self.max_rows), ("wheel_rotate", self.wheel_rotate), ("keep_rot", self.keep_rot),
                         ("anim", self.anim)]),
            ("startup", [("show_seq", self.show_seq), ("after_seq", self.after_seq), ("startup", self.startup)]),
            ("obs", [("obs_host", self.host), ("obs_port", self.port), ("obs_password", self.password), ("scene", self.scene)]),
        )
        v = QVBoxLayout(w)
        labels = []
        self._forms = {}
        for name, rows in groups:
            g = QGroupBox(tr(f"gen.grp.{name}"))
            f = self._forms[name] = QFormLayout(g)
            f.setRowWrapPolicy(QFormLayout.WrapLongRows)   # 見出しが長い言語では入力欄を次の行へ
            for key, widget in rows:
                labels.append(QLabel(tr(f"gen.{key}")))
                f.addRow(labels[-1], widget)
            v.addWidget(g)
        v.addStretch()
        width = max(lb.sizeHint().width() for lb in labels)
        for lb in labels:   # 枠ごとに見出しの幅が違うと入力欄の左端がずれて読みにくい
            lb.setMinimumWidth(width)
        scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.NoFrame)   # 縦に入り切らない画面ではスクロール
        scroll.setWidget(w)
        return scroll

    # ================= カラーエディター =================
    def _color_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.opacity = QSlider(Qt.Horizontal, minimum=20, maximum=100, singleStep=5, pageStep=10, value=self.cfg["opacity"])
        opacity_label = QLabel(f"{self.opacity.value()}%")
        self.opacity.valueChanged.connect(lambda v: (opacity_label.setText(f"{v}%"), self._preview_timer.start()))
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self.opacity, 1)
        opacity_row.addWidget(opacity_label)
        self.color_mode = self._combo("color_mode", config.COLOR_MODES, self.cfg["color_mode"])
        self.colors_box = self._colors_box()
        self.color_mode.currentIndexChanged.connect(self._color_mode_changed)
        self.colors_box.setVisible(self.cfg["color_mode"] == "custom")
        f.addRow(_hint(tr("color.live_hint")))
        f.addRow(tr("gen.opacity"), opacity_row)
        for key, widget in (("color_mode", self.color_mode), ("colors", self.colors_box)):
            f.addRow(tr(f"gen.{key}"), widget)
        scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.NoFrame)
        scroll.setWidget(w)
        return scroll

    def _color_mode_changed(self) -> None:
        self.colors_box.setVisible(self.color_mode.currentData() == "custom")
        self._preview_timer.start()

    def _colors_box(self) -> QWidget:
        """色ごとのボタン（押すと色を選ぶ）+ 既定に戻す。選んだ色はすぐランチャーに反映し、保存は OK を押したとき。"""
        self.colors = dict(self.cfg["colors"])
        self.color_btns = {}
        box = QWidget()
        g = QGridLayout(box)
        g.setContentsMargins(0, 0, 0, 0)
        for i, key in enumerate(theme.CUSTOM_KEYS):
            b = QPushButton(tr(f"color.{key}"))
            b.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            self.color_btns[key] = b
            g.addWidget(b, i // 2, i % 2)
        reset = QPushButton(tr("color.reset"))
        reset.clicked.connect(self._reset_colors)
        g.addWidget(reset, (len(theme.CUSTOM_KEYS) + 1) // 2, 0)
        self.template = QComboBox()
        self.template.addItem(tr("color.template"), "")
        for name in theme.TEMPLATES:
            self.template.addItem(tr(f"color.template.{name}"), name)
        self.template.activated.connect(self._apply_template)
        g.addWidget(self.template, (len(theme.CUSTOM_KEYS) + 1) // 2, 1)
        self._paint_color_btns()
        return box

    def _paint_color_btns(self) -> None:
        for key, b in self.color_btns.items():
            c = QColor(self.colors.get(key, theme.DEFAULT_C[key]))
            fg = "#000000" if c.lightness() > 127 else "#ffffff"   # 文字が背景に埋もれないように
            b.setStyleSheet(f"background: {c.name()}; color: {fg};")
            b.setText(f"{tr(f'color.{key}')}  {c.name()}")
        self._preview_timer.start()

    def _pick_color(self, key: str) -> None:
        """選んでいる途中の色もランチャーに反映する。キャンセルしたら元の色に戻す。"""
        old = self.colors.get(key)
        dlg = QColorDialog(QColor(self.colors.get(key, theme.DEFAULT_C[key])), self)
        dlg.setWindowTitle(tr(f"color.{key}"))

        def live(c: QColor) -> None:
            self.colors[key] = c.name()
            self._paint_color_btns()
        dlg.currentColorChanged.connect(live)
        if dlg.exec() == QDialog.Accepted:
            self.colors[key] = dlg.selectedColor().name()
        elif old is None:
            self.colors.pop(key, None)
        else:
            self.colors[key] = old
        self._paint_color_btns()

    def _apply_template(self) -> None:
        name = self.template.currentData()
        self.template.setCurrentIndex(0)   # 同じテンプレートをもう一度選べるように見出しへ戻す
        if name and (not self.colors or confirm(self, tr("color.template"), tr("color.template_confirm"))):
            self.colors = dict(theme.TEMPLATES[name])
            self._paint_color_btns()

    def _reset_colors(self) -> None:
        if confirm(self, tr("color.reset"), tr("color.reset_confirm")):
            self.colors = {}
            self._paint_color_btns()

    def _fill_mini_icon(self, apps: list | None = None, current: str | None = None) -> None:
        """畳んだときのアイコンの候補 = DDLauncher（既定）+ パスを入れたアプリ。"""
        if apps is None:
            apps = [self._row_app(r) for r in range(self.table.rowCount())]
        if current is None:
            current = self.mini_icon.currentData() or ""
        self.mini_icon.clear()
        self.mini_icon.addItem(theme.app_icon(), tr("gen.mini_icon.default"), "")
        seen = set()
        for a in apps:
            if a.get("path") and a["path"] not in seen:
                seen.add(a["path"])
                self.mini_icon.addItem(load_app_icon(a) or QIcon(), a.get("name") or a["path"], a["path"])
        self.mini_icon.setCurrentIndex(max(0, self.mini_icon.findData(current)))

    def _collect(self) -> None:
        """画面の内容を self.cfg に反映する（保存やスタートアップ登録はしない）。"""
        self.lcfg["apps"] = [self._row_app(r) for r in range(self.table.rowCount())]
        self.lcfg.update(corner=self.corner.currentData(), screen=self.screen.value(), margin=self.margin.value(),
                         dnd_flush=self.dnd_flush.isChecked(), collapse=self.collapse.currentData(),
                         mini_icon=self.mini_icon.currentData() or "",
                         layout=self.layout_mode.currentData(),
                         max_cols=self.max_cols.value(), max_rows=self.max_rows.value(),
                         group_max_rows=self.group_rows.value(), group_max_cols=self.group_cols.value(), keep_rotation=self.keep_rot.isChecked(),
                         wheel_rotate=self.wheel_rotate.isChecked(),
                         show_seq_button=self.show_seq.isChecked(), after_sequence=self.after_seq.currentData(),
                         scale=self.scale.value() / 100)
        self.cfg.update(language=self.language.currentData(), ip_mode=self.ip_mode.currentData(), opacity=self.opacity.value(),
                        theme="opaque" if self.opacity.value() >= 100 else "transparent",   # 旧版に戻したときも近い見た目に
                        color_mode=self.color_mode.currentData(), colors=dict(self.colors),
                        animation=self.anim.currentData(), debug=self.debug_btn.isChecked())
        self.cfg["obs"].update(host=self.host.text().strip(), port=self.port.value(),
                               password=self.password.text(), target_scene=self.scene.text().strip())
        self.cfg["ports"] = [{"port": self.ports.item(r, 0).text(), "memo": self.ports.item(r, 1).text(),
                              "process": self.ports.item(r, 2).text().strip()}
                             for r in range(self.ports.rowCount())
                             if self.ports.item(r, 0).data(Qt.UserRole) != OTHER]   # 不正な番号は normalize で除外
        self.cfg["sound"].update(profiles=self.snd_profiles, auto_restore=self.snd_auto.isChecked(),
                                 with_levels=self.snd_levels.isChecked())
        header = self.table.horizontalHeader()
        self.cfg["ui"] = {"app_col_widths": _col_widths(self.table), "port_col_widths": _col_widths(self.ports),
                          "app_col_order": [header.logicalIndex(v) for v in range(header.count())],
                          "app_split": self.app_split.sizes()}

    def accept(self):
        self._collect()
        try:
            winproc.set_startup(self.startup.isChecked())
        except OSError as e:
            message(self, tr("gen.startup"), tr("gen.startup_failed", e=e), warn=True)
        super().accept()

    # ================= サウンドデバイス =================
    def _sound_tab(self) -> QWidget:
        snd = self.cfg["sound"]
        self.snd_profiles = copy.deepcopy(snd["profiles"])
        self.snd_devices: list[dict] = []
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_hint(tr("snd.hint")))
        row = QHBoxLayout()
        self.snd_now = QLabel(wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
        row.addWidget(self.snd_now, 1)
        b = QPushButton(tr("snd.refresh"))
        b.clicked.connect(self._snd_refresh)
        row.addWidget(b, 0, Qt.AlignTop)
        v.addLayout(row)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.snd_list = QListWidget()
        self.snd_list.currentRowChanged.connect(self._snd_show)
        left.addWidget(self.snd_list, 1)
        for key, slot in (("snd.apply", self._snd_apply), ("snd.save_new", self._snd_save_new),
                          ("snd.overwrite", self._snd_overwrite), ("snd.rename", self._snd_rename),
                          ("snd.delete", self._snd_delete), ("snd.import", self._snd_import)):
            btn = QPushButton(tr(key))
            btn.clicked.connect(slot)
            left.addWidget(btn)
        body.addLayout(left, 1)
        self.snd_table = QTableWidget(0, 5, editTriggers=QAbstractItemView.NoEditTriggers)
        self.snd_table.setHorizontalHeaderLabels([tr(f"snd.col.{k}") for k in ("device", "flow", "default", "mute", "volume")])
        self.snd_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.snd_table.verticalHeader().setVisible(False)
        self.snd_table.setAlternatingRowColors(True)
        body.addWidget(self.snd_table, 3)
        v.addLayout(body, 1)
        self.snd_auto = WrapCheck(tr("snd.auto"), snd["auto_restore"])
        self.snd_levels = WrapCheck(tr("snd.with_levels"), snd["with_levels"])
        v.addWidget(self.snd_auto)
        v.addWidget(self.snd_levels)
        self._snd_refresh()
        return w

    def _snd_refresh(self) -> None:
        """今の既定デバイスを表示し、デバイス一覧を取り直す（プロファイルの「この PC に無い」判定に使う）。"""
        try:
            self.snd_devices, now = sound.list_devices(), sound.get_defaults()
        except OSError as e:
            self.snd_now.setText(tr("snd.failed", e=e))
            return
        label = {d["id"]: d["label"] or d["name"] for d in self.snd_devices}
        self.snd_now.setText(tr("snd.now", out=label.get(now["render"].get("console"), "-"),
                                inp=label.get(now["capture"].get("console"), "-")))
        self.snd_current = sound.capture_profile(tr("snd.current"), self.snd_devices, now)
        self._snd_fill(self.snd_list.currentRow())

    def _snd_fill(self, select: int = 0) -> None:
        """一覧の先頭は「今の状態」（タブを開いたときに今のデバイスが見えるように）。プロファイル k は k+1 行目。"""
        self.snd_list.blockSignals(True)
        self.snd_list.clear()
        self.snd_list.addItem(tr("snd.current"))
        for p in self.snd_profiles:
            active = p["name"] == self.cfg["sound"]["active"]
            self.snd_list.addItem(("● " if active else "") + p["name"])   # ● = 適用中
        self.snd_list.blockSignals(False)
        self.snd_list.setCurrentRow(min(max(select, 0), len(self.snd_profiles)))
        self._snd_show(self.snd_list.currentRow())

    def _snd_show(self, row: int) -> None:
        """選んだプロファイルの中身。既定にするデバイスを上に、この PC に無いデバイスは灰色で出す。"""
        self.snd_table.setRowCount(0)
        p = getattr(self, "snd_current", None) if row == 0 else \
            self.snd_profiles[row - 1] if 0 < row <= len(self.snd_profiles) else None
        if p is None:
            return
        label = {d["id"]: d["label"] or d["name"] for d in self.snd_devices}
        roles = {}
        for flow, rs in p.get("defaults", {}).items():
            for role, e in rs.items():
                kind = "snd.role.comm" if role == "communications" else "snd.role.default"
                if role != "multimedia":   # Windows の画面に合わせ「既定」「通信」の 2 つで見せる
                    roles.setdefault(e.get("id"), []).append(tr(kind))
        levels = sorted(p.get("levels", []), key=lambda l: (l.get("id") not in roles, l.get("flow") != "render", l.get("name", "")))
        for lv in levels:
            dev_id = sound.resolve(lv, self.snd_devices)
            name = label.get(dev_id) or lv.get("name", "") + "  " + tr("snd.missing")
            vol = lv.get("volume")
            cells = [name, tr(f"snd.flow.{lv.get('flow', 'render')}"), " / ".join(roles.get(lv.get("id"), [])),
                     "✓" if lv.get("mute") else "", "" if vol is None else f"{round(vol * 100)}%"]
            r = self.snd_table.rowCount()
            self.snd_table.insertRow(r)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if dev_id is None:
                    item.setForeground(QColor(PORT_COLORS["free"]))
                self.snd_table.setItem(r, c, item)
        self.snd_table.resizeColumnsToContents()
        self.snd_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    def _snd_selected(self) -> dict | None:
        row = self.snd_list.currentRow() - 1   # 先頭行は「今の状態」
        if 0 <= row < len(self.snd_profiles):
            return self.snd_profiles[row]
        message(self, tr("set.tab.sound"), tr("snd.select_none"))
        return None

    def _snd_ask_name(self, title: str, text: str) -> str:
        dlg = QInputDialog(self, windowTitle=title, labelText=tr("snd.name"), textValue=text)
        dlg.setOkButtonText(tr("btn.ok"))
        dlg.setCancelButtonText(tr("btn.cancel"))
        return dlg.textValue().strip() if dlg.exec() else ""

    def _snd_unique(self, name: str, skip: dict | None = None) -> str:
        return unique_name(name, {p["name"] for p in self.snd_profiles if p is not skip})

    def _snd_capture(self, name: str) -> dict | None:
        try:
            return sound.capture_profile(name, sound.list_devices(), sound.get_defaults())
        except OSError as e:
            message(self, tr("set.tab.sound"), tr("snd.failed", e=e), warn=True)
            return None

    def _snd_apply(self) -> None:
        p = self._snd_selected()
        if p:
            self.sound_apply.emit(p, self.snd_levels.isChecked())   # 呼び出し側が Windows に反映して、適用中として覚える
            self.cfg["sound"]["active"] = p["name"]
            self._snd_refresh()

    def _snd_save_new(self) -> None:
        name = self._snd_ask_name(tr("snd.save_new"), tr("snd.default_name", n=len(self.snd_profiles) + 1))
        p = self._snd_capture(self._snd_unique(name)) if name else None
        if p:
            self.snd_profiles.append(p)
            self._snd_fill(len(self.snd_profiles))

    def _snd_overwrite(self) -> None:
        p = self._snd_selected()
        if p and confirm(self, tr("snd.overwrite"), tr("snd.overwrite_confirm", name=p["name"])):
            new = self._snd_capture(p["name"])
            if new:
                p.update(new)
                self._snd_fill(self.snd_list.currentRow())

    def _snd_rename(self) -> None:
        p = self._snd_selected()
        name = self._snd_ask_name(tr("snd.rename"), p["name"]) if p else ""
        if name:
            new = self._snd_unique(name, skip=p)
            if self.cfg["sound"]["active"] == p["name"]:
                self.cfg["sound"]["active"] = new
            p["name"] = new
            self._snd_fill(self.snd_list.currentRow())

    def _snd_delete(self) -> None:
        p = self._snd_selected()
        if p and confirm(self, tr("snd.delete"), tr("snd.delete_confirm", name=p["name"])):
            self.snd_profiles.remove(p)   # OK を押すまで保存しない（保存前の設定は自動バックアップにも残る）
            if self.cfg["sound"]["active"] == p["name"]:
                self.cfg["sound"]["active"] = ""
            self._snd_fill(self.snd_list.currentRow())

    def _snd_import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("snd.import"), self.cfg["sound"]["spr_dir"], tr("snd.spr_filter"))
        failed = []
        for path in paths:
            try:
                p = sound.parse_spr(sound.read_text(path), "")
            except OSError as e:
                failed.append(f"{os.path.basename(path)}: {e}")
                continue
            p["name"] = self._snd_unique(os.path.splitext(os.path.basename(path))[0])
            self.snd_profiles.append(p)
        if paths:
            self.cfg["sound"]["spr_dir"] = os.path.dirname(paths[0])   # 次回は同じフォルダから開く
            self._snd_fill(len(self.snd_profiles))
        if failed:
            message(self, tr("snd.import"), tr("dbg.failed", e="\n".join(failed)), warn=True)

    # ================= 情報 / Debug Mode =================
    def _info_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout()
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.language = QComboBox()
        self.language.addItem(tr("lang.auto"), "auto")
        for code, name in i18n.LANGS.items():
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(self.cfg["language"])))
        f.addRow(tr("gen.language"), self.language)
        f.addRow(tr("info.version"), QLabel(about.VERSION))
        f.addRow(tr("info.build_date"), QLabel(about.format_date(about.build_date(), i18n.current())))
        f.addRow(tr("info.developer"), QLabel(about.DEVELOPER))   # 設定画面の中だけ（常に見えるランチャーには出さない）
        self.debug_btn = QPushButton(checkable=True, checked=self.cfg["debug"])
        self.debug_btn.toggled.connect(self._show_debug)
        f.addRow("Debug Mode", self.debug_btn)

        self.debug_box = QWidget()
        d = QVBoxLayout(self.debug_box)
        d.setContentsMargins(0, 0, 0, 0)
        d.addWidget(self._paths_group())
        d.addWidget(self._backup_group())
        d.addWidget(self._nexus_group())
        d.addWidget(self._uninstall_group())
        d.addStretch()

        lay = QVBoxLayout(w)
        lay.addLayout(f)
        lay.addWidget(self.debug_box)
        lay.addStretch()
        self._show_debug(self.cfg["debug"])
        scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.NoFrame)
        scroll.setWidget(w)
        return scroll

    def _show_debug(self, on: bool) -> None:
        self.debug_btn.setText("ON" if on else "OFF")
        self.debug_box.setVisible(on)
        self.tabs.setTabVisible(self.sound_tab_index, on)
        for w in (self.max_cols, self.max_rows):   # LINEAR は調整中なので Debug Mode のときだけ見せる
            self._forms["display"].setRowVisible(w, on)
        self.layout_mode.view().setRowHidden(config.LAYOUTS.index("linear"), not on)

    # ---- 保存場所 ----
    def _paths_group(self) -> QGroupBox:
        g = QGroupBox(tr("dbg.paths"))
        f = QFormLayout(g)
        f.setRowWrapPolicy(QFormLayout.WrapLongRows)
        rows = [("dbg.path.app", about.app_location()), ("dbg.path.config", config.CONFIG_DIR)]
        if about.legacy_exists():
            rows.append(("dbg.path.legacy", config.LEGACY_DIR))
        for key, path in rows:
            row = QHBoxLayout()
            lb = QLabel(path, wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
            row.addWidget(lb, 1)
            b = QPushButton(tr("dbg.open"))
            b.clicked.connect(lambda _=False, p=path: self._open_folder(p))
            row.addWidget(b)
            f.addRow(tr(key), row)
        return g

    def _open_folder(self, path: str) -> None:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if os.path.isdir(folder):
            os.startfile(folder)   # エクスプローラーで開くだけ。中身には触らない

    # ---- バックアップ（エクスポート / インポート） ----
    def _backup_group(self) -> QGroupBox:
        g = QGroupBox(tr("dbg.backup"))
        v = QVBoxLayout(g)
        v.addWidget(_hint(tr("dbg.backup.hint")))
        self.sec_checks = {}
        row = QHBoxLayout()
        for s in config.SECTIONS:
            cb = QCheckBox(tr(f"sec.{s}"), checked=True)
            self.sec_checks[s] = cb
            row.addWidget(cb)
        row.addStretch()
        v.addLayout(row)
        btns = QHBoxLayout()
        for key, slot in (("dbg.export", self._export), ("dbg.import", self._import)):
            b = QPushButton(tr(key))
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        v.addLayout(btns)
        return g

    def _export(self) -> None:
        sections = [s for s, cb in self.sec_checks.items() if cb.isChecked()]
        if not sections:
            message(self, tr("dbg.export_title"), tr("dbg.select_none"))
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("dbg.export_title"), "DDLauncher-settings.json",
                                              tr("dbg.json_filter"))   # 上書きするときは Windows が確認を出す
        if not path:
            return
        self._collect()   # 画面で変えたばかりの内容も含める
        try:
            config.write_json(path, config.export_data(config.normalize(self.cfg), sections))
        except OSError as e:
            message(self, tr("dbg.export_title"), tr("dbg.failed", e=e), warn=True)
            return
        message(self, tr("dbg.export_title"), tr("dbg.exported", path=path))

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("dbg.import_title"), "", tr("dbg.json_filter"))
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fp:
                data = json.load(fp)
        except (OSError, ValueError) as e:
            message(self, tr("dbg.import_title"), tr("dbg.failed", e=e), warn=True)
            return
        wanted = [s for s, cb in self.sec_checks.items() if cb.isChecked()]
        if isinstance(data, dict):   # 選んだ項目だけを読む
            data = dict(data, sections=[s for s in data.get("sections", list(config.SECTIONS)) if s in wanted])
        new_cfg, found = config.import_data(config.load(), data)
        if not found:
            message(self, tr("dbg.import_title"), tr("dbg.import_empty"))
            return
        names = "\n".join("- " + tr(f"sec.{s}") for s in found)
        if not confirm(self, tr("dbg.import_title"), tr("dbg.import_confirm", sections=names)):
            return
        try:
            config.backup_current()   # 読み込む前の設定を backups に残す
        except OSError as e:
            message(self, tr("dbg.import_title"), tr("dbg.failed", e=e), warn=True)
            return
        self.imported = new_cfg
        self.reject()   # この画面の内容では保存しない。呼び出し側が読み込んだ設定で作り直す

    # ---- NexusFont の引っ越し ----
    def _nexus_group(self) -> QGroupBox:
        g = QGroupBox(tr("nf.title"))
        v = QVBoxLayout(g)
        v.addWidget(_hint(tr("nf.group.hint")))
        row = QHBoxLayout()
        b = QPushButton(tr("nf.open"))
        b.clicked.connect(self._open_nexus)
        row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        return g

    def _open_nexus(self) -> None:
        from nexus_dialog import NexusDialog   # nexus_dialog が settings_dialog の部品を使うので、ここで読む
        NexusDialog(self).exec()

    # ---- アンインストールの案内 ----
    def _uninstall_group(self) -> QGroupBox:
        g = QGroupBox(tr("dbg.uninstall"))
        v = QVBoxLayout(g)
        legacy = tr("dbg.uninstall.legacy", path=config.LEGACY_DIR) if about.legacy_exists() else ""
        text = tr("dbg.uninstall.body", app=about.app_location(), config=config.CONFIG_DIR,
                  legacy=legacy, parent=os.path.dirname(config.CONFIG_DIR))
        v.addWidget(QLabel(text, wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse))
        v.addWidget(_hint(tr("dbg.uninstall.tool")))
        return g
