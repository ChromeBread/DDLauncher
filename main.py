"""DDLauncher エントリポイント。

  pythonw main.py              通常起動
  pythonw main.py --autostart  起動後すぐ各ランチャーの起動シーケンスを開始（スタートアップ登録時）
"""
import copy
import os
import sys

from PySide6.QtCore import QLockFile, QObject, QTimer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import config
import i18n
import sound
import theme
import winproc
from launcher_window import LauncherWindow
from obs_bridge import ObsStatus
from settings_dialog import SettingsDialog, message

tr = i18n.tr

POLL_MS = 2000   # 起動状態の確認間隔


class Controller(QObject):
    """設定を持ち、ランチャーウィンドウ群を生成・再生成する。"""

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.windows: list[LauncherWindow] = []
        self.obs_state = None
        self.settings_dlg = None   # 開いている設定画面（2 つ目を開かない）
        self._sound_seen = None   # 最後に見た既定デバイス（変わったときだけ戻す処理をする）
        self.obs_status = ObsStatus(lambda: self.cfg["obs"], self)
        self.obs_status.updated.connect(self.on_obs)
        self.poll = QTimer(self, interval=POLL_MS, timeout=self.poll_status)
        # トレイ: 「隠す」にしたランチャーの復帰口
        self.tray_menu = QMenu()
        self.tray = QSystemTrayIcon(theme.app_icon(), self)
        self.tray.setToolTip("DDLauncher")
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and self.restore_all())
        self.tray.show()
        self.build()
        self.poll.start()

    def restore_all(self) -> None:
        for w in self.windows:
            w.restore()

    def _fill_tray_menu(self) -> None:
        m = self.tray_menu
        m.clear()   # 言語を切り替えたら作り直す
        m.addAction(tr("tray.show"), self.restore_all)
        m.addAction(tr("tray.start_seq"), self.start_all)
        snd = self.cfg["sound"]
        if snd["profiles"]:   # よく切り替えるので、設定画面を開かずに選べるように
            sm = m.addMenu(tr("tray.sound"))
            for p in snd["profiles"]:
                a = sm.addAction(p["name"], lambda p=p: self.apply_sound(p))
                a.setCheckable(True)
                a.setChecked(p["name"] == snd["active"])
        m.addSeparator()
        m.addAction(tr("tray.quit"), QApplication.quit)

    def build(self) -> None:
        theme.apply_colors(self.cfg["colors"] if self.cfg["color_mode"] == "custom" else None, self.cfg["opacity"])   # 窓を作る前に（色は作るときに読む）
        self._fill_tray_menu()
        for w in self.windows:
            w.shutdown()
        self.windows = []
        for lcfg in self.cfg["launchers"]:
            w = LauncherWindow(lcfg, self.cfg)
            w.changed.connect(self.save)
            w.settings_requested.connect(self.open_settings)
            w.quit_requested.connect(QApplication.quit)
            w.show()
            w.set_obs(self.obs_state)
            self.windows.append(w)
        self.poll_status()

    def on_obs(self, state) -> None:
        self.obs_state = state
        for w in self.windows:
            w.set_obs(state)

    def poll_status(self) -> None:
        snap = winproc.snapshot()   # 全ランチャーで 1 回のスナップショットを共有
        for w in self.windows:
            w.update_status(snap)
        self.check_sound()

    def apply_sound(self, prof: dict, with_levels: bool | None = None) -> None:
        """サウンドプロファイルを適用し、適用中として覚える（自動で戻す機能の戻し先になる）。"""
        if with_levels is None:
            with_levels = self.cfg["sound"]["with_levels"]
        try:
            _, missing = sound.apply_profile(prof, sound.list_devices(), sound.set_default, sound.set_level, with_levels)
        except OSError as e:
            message(None, "DDLauncher", tr("snd.failed", e=e), warn=True)
            return
        # 適用したプロファイルはその場で保存する（設定画面をキャンセルしても「適用中」の中身が消えないように）
        profiles = self.cfg["sound"]["profiles"]
        i = next((k for k, p in enumerate(profiles) if p["name"] == prof["name"]), None)
        if i is None:
            profiles.append(copy.deepcopy(prof))
        else:
            profiles[i] = copy.deepcopy(prof)
        self.cfg["sound"]["active"] = prof["name"]
        self._sound_seen = None
        self.save()
        QTimer.singleShot(0, self._fill_tray_menu)   # メニューの操作中に作り直さない
        for w in self.windows:
            w.set_status(tr("snd.applied", name=prof["name"]))
        if missing:
            message(None, "DDLauncher", tr("snd.missing_list", names="\n".join("- " + n for n in missing)))

    def check_sound(self) -> None:
        """「勝手に切り替わったら戻す」: 既定デバイスが変わったときだけ、適用中のプロファイルに戻す。"""
        snd = self.cfg["sound"]
        prof = next((p for p in snd["profiles"] if p["name"] == snd["active"]), None)
        if not snd["auto_restore"] or prof is None:
            self._sound_seen = None
            return
        try:
            now = sound.get_defaults()
            if now == self._sound_seen:
                return
            tg, _ = sound.targets(prof, sound.list_devices())   # デバイス一覧は変わったときだけ取り直す
            fixed = [(role, dev_id) for (flow, role), dev_id in tg.items() if now[flow].get(role) != dev_id]
            for role, dev_id in fixed:
                sound.set_default(dev_id, sound.ROLES[role])
            self._sound_seen = sound.get_defaults() if fixed else now
        except OSError:
            return   # 戻せなくても次の確認でもう一度試す
        if fixed:
            for w in self.windows:
                w.set_status(tr("snd.restored", name=prof["name"]))

    def save(self) -> None:
        try:
            config.save(self.cfg)
        except OSError as e:
            message(None, "DDLauncher", tr("err.save_failed", e=e), warn=True)

    def open_settings(self, window: LauncherWindow) -> None:
        if self.settings_dlg is not None:   # 開いている設定画面を前に出す
            self.settings_dlg.showNormal()
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
            return
        if any(w.seq.running for w in self.windows):
            window.set_status(tr("status.settings_blocked"))
            return
        dlg = self.settings_dlg = SettingsDialog(self.windows.index(window), self.cfg, window)
        dlg.colors_preview.connect(self.preview_colors)
        dlg.sound_apply.connect(self.apply_sound)
        dlg.dnd_reset.connect(window.reset_panel)
        window.show_beside(dlg)
        try:
            accepted = dlg.exec()
        finally:
            self.settings_dlg = None
        new_cfg = dlg.imported if dlg.imported is not None else (dlg.cfg if accepted else None)   # インポートは OK と同じ扱い
        if new_cfg is None:   # キャンセル: 試していた色を元に戻す
            self.preview_colors(self.cfg["colors"] if self.cfg["color_mode"] == "custom" else None, self.cfg["opacity"])
        else:
            self.cfg = config.normalize(new_cfg)
            i18n.set_language(self.cfg["language"])
            self.save()
            QTimer.singleShot(0, self.build)   # テーマ等を反映するため作り直す（シグナル処理中に消さない）

    def preview_colors(self, colors: dict | None, opacity: int) -> None:
        theme.apply_colors(colors, opacity)
        for w in self.windows:
            w.refresh_colors()

    def start_all(self) -> None:
        for w in self.windows:
            w.start_sequence()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(theme.app_icon())

    # 二重起動防止（シーケンスで同じアプリを 2 回起動しないため）
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    lock = QLockFile(os.path.join(config.CONFIG_DIR, "ddlauncher.lock"))
    if not lock.tryLock(100):
        return 0
    # 旧バージョン（%APPDATA%\DDLauncher のロックを使う）と同時に動かさない。旧フォルダが無ければ作らない
    legacy_lock = None
    if os.path.isdir(config.LEGACY_DIR):
        legacy_lock = QLockFile(os.path.join(config.LEGACY_DIR, "ddlauncher.lock"))
        if not legacy_lock.tryLock(100):
            return 0

    cfg = config.load()
    i18n.set_language(cfg["language"])
    ctrl = Controller(cfg)
    if "--autostart" in sys.argv:
        ctrl.start_all()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
