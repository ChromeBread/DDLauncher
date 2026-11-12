"""NexusFont の引っ越し画面（Debug Mode）。数える・書き出す・読み込むはすべて別スレッドで行い、画面を固めない。"""
import os
import shutil
import threading
import time
import zipfile

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

import config
import i18n
import nexusfont
import winproc
from obs_bridge import fmt_bytes
from settings_dialog import _hint, confirm, message

tr = i18n.tr


class _Bus(QObject):
    """ワーカースレッド → 画面。emit は Qt がメインスレッドへ配送する。"""
    scanned = Signal(object, object)    # (ツリーの項目, [(path, size)])
    progress = Signal(int, int, str)    # (済んだバイト, 全体, いまのファイル)
    finished = Signal(str, object)      # (処理名, 結果 / 例外)


class NexusDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("nf.title"))
        self.resize(760, 520)
        self.bus = _Bus(self)
        self.bus.scanned.connect(self._on_scanned)
        self.bus.progress.connect(self._on_progress)
        self.bus.finished.connect(self._on_finished)
        self.cancel_flag = threading.Event()
        self.busy = False
        self.files: dict = {}   # ツリーの項目 → [(path, size)]（セットは見つかったフォントだけ）
        self.lib = None
        self.userdata = nexusfont.find_userdata()

        lay = QVBoxLayout(self)
        lay.addWidget(_hint(tr("nf.notice")))
        row = QHBoxLayout()
        self.loc = QLabel(wordWrap=True, textInteractionFlags=Qt.TextSelectableByMouse)
        row.addWidget(QLabel(tr("nf.location")))
        row.addWidget(self.loc, 1)
        self.change_btn = QPushButton(tr("nf.change"))
        self.change_btn.clicked.connect(self._change_location)
        row.addWidget(self.change_btn)
        lay.addLayout(row)
        wrow = QHBoxLayout()   # 画面外に開くようになった NexusFont を戻す
        wrow.addWidget(_hint(tr("nf.window_hint")), 1)
        self.window_btn = QPushButton(tr("nf.window_fix"))
        self.window_btn.clicked.connect(lambda: self._fix_window(ask=False))
        wrow.addWidget(self.window_btn, 0, Qt.AlignTop)
        lay.addLayout(wrow)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("nf.col.name"), tr("nf.col.files"), tr("nf.col.size")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemChanged.connect(lambda *_: self._update_total())
        lay.addWidget(self.tree, 1)
        self.total = QLabel()
        lay.addWidget(self.total)

        self.bar = QProgressBar(maximum=1000, visible=False)
        self.status = QLabel(wordWrap=True)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        btns = QHBoxLayout()
        self.mode = QComboBox()   # 書き出し方。読み込むときは zip に入っている印で決まる
        for key in ("move", "asis"):
            self.mode.addItem(tr(f"nf.mode.{key}"), key == "asis")
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel(tr("nf.mode")))
        mrow.addWidget(self.mode, 1)
        lay.insertLayout(lay.count() - 2, mrow)   # 合計欄の下、進捗バーの上
        self.export_btn = QPushButton(tr("nf.export"))
        self.export_btn.clicked.connect(self._export)
        self.import_btn = QPushButton(tr("nf.import"))
        self.import_btn.clicked.connect(self._import)
        self.cancel_btn = QPushButton(tr("nf.cancel"), visible=False)
        self.cancel_btn.clicked.connect(self.cancel_flag.set)
        self.close_btn = QPushButton(tr("nf.close"))
        self.close_btn.clicked.connect(self.close)
        for b in (self.export_btn, self.import_btn):
            btns.addWidget(b)
        btns.addStretch()
        for b in (self.cancel_btn, self.close_btn):
            btns.addWidget(b)
        lay.addLayout(btns)
        self._load_library()

    # ---- ライブラリの一覧 ----
    def _change_location(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("nf.pick_folder"))
        if folder:
            self.userdata = nexusfont.userdata_of(os.path.normpath(folder))
            self._load_library()

    def _load_library(self) -> None:
        self.tree.clear()
        self.files.clear()
        self.lib = None
        self.loc.setText(self.userdata or tr("nf.not_found"))
        if self.userdata and os.path.isfile(os.path.join(self.userdata, "library.xml")):
            try:
                self.lib = nexusfont.read_library(self.userdata)
            except (OSError, nexusfont.ET.ParseError) as e:
                self.status.setText(tr("dbg.failed", e=e))
        if self.lib is None:
            self._update_total()
            return
        self.tree.blockSignals(True)
        for key, names, checked in (("nf.sets", list(self.lib["sets"]), True), ("nf.folders", self.lib["folders"], False)):
            top = QTreeWidgetItem(self.tree, [tr(key)])
            top.setFlags(top.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            for name in names:
                it = QTreeWidgetItem(top, [name, tr("nf.scanning"), ""])
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                it.setData(0, Qt.UserRole, (key, name))
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self.tree.resizeColumnToContents(1)
        items = [(c, *c.data(0, Qt.UserRole)) for t in range(2) for c in
                 (self.tree.topLevelItem(t).child(i) for i in range(self.tree.topLevelItem(t).childCount()))]
        threading.Thread(target=self._scan, args=(items,), daemon=True).start()
        self._update_total()

    def _scan(self, items) -> None:
        lib = self.lib
        for it, kind, name in items:   # Qt の項目には触らず、結果だけ画面へ送る
            if kind == "nf.sets":
                found = []
                for p in lib["sets"][name]:
                    try:
                        found.append((p, os.path.getsize(nexusfont.lp(p))))
                    except OSError:
                        pass
            else:
                found = nexusfont.scan_folder(name)
            if self.lib is not lib:
                return   # 場所を変えたので古い一覧は捨てる
            self.bus.scanned.emit(it, found)

    def _on_scanned(self, it, found) -> None:
        if it.treeWidget() is not self.tree:
            return
        kind, name = it.data(0, Qt.UserRole)
        self.files[id(it)] = found
        n = len(found)
        missing = len(self.lib["sets"][name]) - n if kind == "nf.sets" else 0
        self.tree.blockSignals(True)
        it.setText(1, tr("nf.files_missing", n=f"{n:,}", missing=missing) if missing else f"{n:,}")
        it.setText(2, fmt_bytes(sum(s for _, s in found)))
        it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        it.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.blockSignals(False)
        self._update_total()

    def _checked(self) -> list:
        out = []
        for t in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(t)
            out += [top.child(i) for i in range(top.childCount()) if top.child(i).checkState(0) == Qt.Checked]
        return out

    def _update_total(self) -> None:
        items = self._checked()
        ready = all(id(it) in self.files for it in items)
        files = sum(len(self.files.get(id(it), [])) for it in items)
        size = sum(s for it in items for _, s in self.files.get(id(it), []))
        self.total.setText(tr("nf.selected", files=f"{files:,}", size=fmt_bytes(size)) + ("" if ready else "  " + tr("nf.scanning")))
        self.export_btn.setEnabled(not self.busy and self.lib is not None and bool(items) and ready)
        self.import_btn.setEnabled(not self.busy)

    # ---- 作業中の表示 ----
    def _start(self, total: int) -> None:
        self.busy, self.total_bytes, self.t0 = True, max(total, 1), time.monotonic()
        self.cancel_flag.clear()
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.cancel_btn.setVisible(True)
        for w in (self.change_btn, self.tree, self.close_btn, self.window_btn):
            w.setEnabled(False)
        self._update_total()

    def _stop(self) -> None:
        self.busy = False
        self.bar.setVisible(False)
        self.cancel_btn.setVisible(False)
        for w in (self.change_btn, self.tree, self.close_btn, self.window_btn):
            w.setEnabled(True)
        self._update_total()

    def _progress_cb(self, total: int):
        last = [0.0]

        def cb(done: int, name: str) -> None:
            now = time.monotonic()
            if now - last[0] >= 0.1 or done >= total:   # 画面の更新は 1 秒に 10 回まで
                last[0] = now
                self.bus.progress.emit(done, total, name)
        return cb

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self.bar.setValue(int(done * 1000 / max(total, 1)))
        speed = done / max(time.monotonic() - self.t0, 0.001)
        self.status.setText(tr("nf.progress", done=fmt_bytes(done), total=fmt_bytes(total), speed=fmt_bytes(int(speed)),
                               eta=self._eta((total - done) / max(speed, 1)), file=name))

    @staticmethod
    def _eta(sec: float) -> str:
        """推定時間を「1 分以内 / 約 N 分 / 約 H 時間 M 分」で。秒まで出すと目安なのに正確そうに見えるため。"""
        m = round(sec / 60)
        if m < 1:
            return tr("nf.eta_under_min")
        return tr("nf.eta_min", m=m) if m < 60 else tr("nf.eta_hm", h=m // 60, m=m % 60)

    def _run(self, kind: str, fn) -> None:
        def work():
            try:
                result = fn()
            except Exception as e:   # 失敗しても画面は戻す（理由は表示する）
                result = e
            self.bus.finished.emit(kind, result)
        threading.Thread(target=work, daemon=True).start()

    def closeEvent(self, e):
        if self.busy:   # 作業中は閉じない（中止ボタンで止める）
            e.ignore()
            return
        self.lib = None   # 数えている途中なら止める
        super().closeEvent(e)

    # ---- エクスポート ----
    def _export(self) -> None:
        items = self._checked()
        sets = [it.data(0, Qt.UserRole)[1] for it in items if it.data(0, Qt.UserRole)[0] == "nf.sets"]
        folders = {it.data(0, Qt.UserRole)[1]: self.files[id(it)] for it in items if it.data(0, Qt.UserRole)[0] == "nf.folders"}
        plan = nexusfont.plan_export(self.lib, sets, folders)
        path, _ = QFileDialog.getSaveFileName(self, tr("nf.export"), time.strftime("NexusFont-%Y%m%d.zip"),
                                              tr("nf.zip_filter"))   # 上書きするときは Windows が確認を出す
        if not path:
            return
        try:
            free = shutil.disk_usage(os.path.dirname(path)).free
        except OSError:
            free = -1
        if 0 <= free < plan["total"] + 50 * 1024 * 1024:
            message(self, tr("nf.export"), tr("nf.no_space", size=fmt_bytes(plan["total"]), free=fmt_bytes(free)), warn=True)
            return
        self.export_plan, self.export_path, self.export_free = plan, path, free
        self._start(0)
        self.status.setText(tr("nf.estimating"))
        self._run("estimate", lambda: nexusfont.measure_speed(plan["entries"], os.path.dirname(path)))

    def _export_start(self, speed: float) -> None:
        plan, path, free = self.export_plan, self.export_path, self.export_free
        if not confirm(self, tr("nf.export"), tr("nf.export_confirm", files=f"{len(plan['entries']):,}",
                                                 size=fmt_bytes(plan["total"]), free=fmt_bytes(max(free, 0)),
                                                 time=self._eta(plan["total"] / max(speed, 1)), speed=fmt_bytes(int(speed)))):
            return
        self.export_missing = plan["missing"]
        self._start(plan["total"])
        cb = self._progress_cb(plan["total"])
        asis = self.mode.currentData()
        self._run("export", lambda: nexusfont.write_export(path, self.userdata, plan, cb, self.cancel_flag.is_set, asis))

    # ---- インポート ----
    def _import(self) -> None:
        if "nexusfont.exe" in winproc.snapshot():
            message(self, tr("nf.import"), tr("nf.running"), warn=True)
            return
        if not self.userdata:
            message(self, tr("nf.import"), tr("nf.not_found_import"), warn=True)
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("nf.import"), "", tr("nf.zip_filter"))
        if not path:
            return
        try:
            zf = zipfile.ZipFile(nexusfont.lp(path))
        except (OSError, zipfile.BadZipFile) as e:
            message(self, tr("nf.import"), tr("dbg.failed", e=e), warn=True)
            return
        manifest = nexusfont.read_manifest(zf)
        if manifest is None:
            zf.close()
            message(self, tr("nf.import"), tr("nf.bad_zip"), warn=True)
            return
        if manifest.get("asis"):   # 元の PC と同じパスへ戻す
            drives = nexusfont.missing_drives(manifest)
            if drives:
                zf.close()
                message(self, tr("nf.import"), tr("nf.missing_drives", drives=", ".join(drives)), warn=True)
                return
            dest = None
        else:
            dest = QFileDialog.getExistingDirectory(self, tr("nf.pick_dest"))
            if not dest:
                zf.close()
                return
            dest = os.path.normpath(dest)
        self.zf, self.manifest, self.dest = zf, manifest, dest
        self._start(0)
        self.status.setText(tr("nf.checking"))
        self._run("plan", lambda: nexusfont.plan_import(zf, manifest, dest, self.cancel_flag.is_set))

    def _import_extract(self, plan: dict) -> None:
        if self.dest is None:   # どこに書くか分かるよう、戻す先のフォルダを見せる（多すぎる分は省く）
            roots = list(self.manifest.get("folders", {})) + sorted({os.path.dirname(p) for m in self.manifest.get("sets", {}).values() for p in m})
            where = "\n".join([tr("nf.asis_dest")] + roots[:10] + (["…"] if len(roots) > 10 else []))
        else:
            where = self.dest
        if not confirm(self, tr("nf.import"), tr("nf.import_confirm", dest=where, new=f"{len(plan['new']):,}",
                                                 same=f"{len(plan['same']):,}")):
            self.zf.close()
            return
        infos = plan["new"]
        if plan["differ"] and confirm(self, tr("nf.import"), tr("nf.overwrite_confirm", n=f"{len(plan['differ']):,}")):
            infos = infos + plan["differ"]
        total = sum(i.file_size for i, _ in infos)
        self._start(total)
        cb = self._progress_cb(total)
        zf = self.zf
        self._run("extract", lambda: nexusfont.extract(zf, infos, config.move_to_trash, cb, self.cancel_flag.is_set))

    def _on_finished(self, kind: str, result) -> None:
        self._stop()
        self.status.setText("")
        if isinstance(result, InterruptedError) or result is False:
            if kind in ("plan", "extract"):
                self.zf.close()
            message(self, tr("nf.title"), tr("nf.cancelled_import" if kind in ("plan", "extract") else "nf.cancelled_export"))
            return
        if isinstance(result, Exception):
            if kind in ("plan", "extract"):
                self.zf.close()
            message(self, tr("nf.title"), tr("dbg.failed", e=result), warn=True)
            return
        if kind == "estimate":
            self._export_start(result)
        elif kind == "export":
            text = tr("dbg.exported", path=self.export_path)
            if self.export_missing:
                shown = self.export_missing[:20]   # 一覧が長くなりすぎないように
                text += "\n\n" + tr("nf.missing_list", n=len(self.export_missing), files="\n".join(shown))
            message(self, tr("nf.export"), text)
        elif kind == "plan":
            self._import_extract(result)
        elif kind == "extract":
            try:
                backup = nexusfont.restore_data(self.zf, self.userdata, self.manifest, self.dest, config.BACKUP_DIR)
            except OSError as e:
                message(self, tr("nf.import"), tr("dbg.failed", e=e), warn=True)
                return
            finally:
                self.zf.close()
            message(self, tr("nf.import"), tr("nf.imported", backup=backup or "-"))
            if self.dest is None:   # 「そのまま」はウィンドウ位置も元の PC のまま
                self._fix_window(ask=True)
            self._load_library()

    def _fix_window(self, ask: bool) -> None:
        """ask=True: 画面外のときだけ確認して直す（インポート直後）/ False: ボタンから。結果を必ず知らせる。"""
        if not ask and "nexusfont.exe" in winproc.snapshot():
            message(self, tr("nf.window_fix"), tr("nf.running_window"), warn=True)
            return
        p = os.path.join(self.userdata or "", "nexusfont.ini")
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                ini = f.read()
        except OSError:
            if not ask:
                message(self, tr("nf.window_fix"), tr("nf.not_found_import"), warn=True)
            return
        mons = sorted(winproc.monitors(), key=lambda m: not m["primary"])   # 主モニタを先頭に
        fixed = nexusfont.window_fix(ini, [m["work"] for m in mons])
        if fixed is None:
            if not ask:
                message(self, tr("nf.window_fix"), tr("nf.window_ok"))
            return
        if ask and not confirm(self, tr("nf.window_fix"), tr("nf.window_offscreen_confirm")):
            return
        try:
            os.makedirs(config.BACKUP_DIR, exist_ok=True)
            shutil.copy2(p, os.path.join(config.BACKUP_DIR, time.strftime("nexusfont-%Y%m%d-%H%M%S.ini")))
            with open(p + ".tmp", "w", encoding="utf-8-sig", newline="") as f:
                f.write(fixed)
            os.replace(p + ".tmp", p)
        except OSError as e:
            message(self, tr("nf.window_fix"), tr("dbg.failed", e=e), warn=True)
            return
        message(self, tr("nf.window_fix"), tr("nf.window_fixed"))
