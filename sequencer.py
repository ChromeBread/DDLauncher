"""安全自動起動: 設定順に「前のアプリから delay_sec 秒後」に起動する。既に起動中のアプリは二重起動しない。"""
import math
import time

from PySide6.QtCore import QObject, QTimer, Signal

import i18n
import winproc


MONITOR_WAIT_SEC = 30


def place_on_monitor(app: dict, parent: QObject) -> None:
    """起動後、ウィンドウが現れるまで 0.5 秒間隔で待ち、指定モニタへ移す。

    shortcut: 最初に見つかったウィンドウだけ移す（スプラッシュ→本体と切り替わるアプリは本体が移らない）。
    困るアプリが出たら、見つかった後も数秒監視を続ける。
    """
    device = app.get("monitor")
    if not device:
        return
    proc = winproc.process_name(app)
    deadline = time.monotonic() + MONITOR_WAIT_SEC
    timer = QTimer(parent, interval=500)   # parent が保持するので GC されない

    def tick():
        hwnds = winproc.find_windows(proc)
        if hwnds or time.monotonic() > deadline:
            timer.stop()
            timer.deleteLater()
            for h in hwnds:
                winproc.move_to_monitor(h, device)

    timer.timeout.connect(tick)
    timer.start()


def timeline(apps: list[dict]) -> list[tuple[int, float, float]]:
    """起動シーケンスで起動する順に [(index, 待ち秒, 開始からの秒)]。Sequencer.start / _arm と同じ数え方。"""
    out, t = [], 0.0
    for i, a in enumerate(apps):
        if a.get("autostart") and a.get("path"):
            try:
                delay = max(0.0, float(a.get("delay_sec", 0)))
            except (TypeError, ValueError):
                delay = 0.0
            t += delay
            out.append((i, delay, t))
    return out


class Sequencer(QObject):
    progress = Signal(int, str)   # (次に起動するタイルの index / -1, 表示テキスト)
    done = Signal(list)           # 最後まで完了（中止時は出ない）。引数は失敗リスト

    def __init__(self, parent=None):
        super().__init__(parent)
        self.apps: list[dict] = []
        self.queue: list[int] = []
        self.errors: list[str] = []
        self.deadline = 0.0
        self.timer = QTimer(self, interval=200, timeout=self._tick)

    @property
    def running(self) -> bool:
        return bool(self.queue)

    def start(self, apps: list[dict]) -> None:
        self.apps = apps
        self.queue = [i for i, a in enumerate(apps) if a.get("autostart") and a.get("path")]
        self.errors = []
        self._arm()

    def cancel(self) -> None:
        self.queue = []
        self.timer.stop()
        self.progress.emit(-1, "SEQ CANCELED")

    def _arm(self) -> None:
        if not self.queue:
            self.timer.stop()
            # 見た目のため英字の合図は残し、失敗の中身だけは各言語で読めるようにする
            msg = "SEQ DONE" + (" / " + i18n.tr("seq.failed", apps=", ".join(self.errors)) if self.errors else "")
            self.progress.emit(-1, msg)
            self.done.emit(list(self.errors))
            return
        self.deadline = time.monotonic() + max(0.0, float(self.apps[self.queue[0]]["delay_sec"]))
        self.timer.start()
        self._tick()

    def _tick(self) -> None:
        if not self.queue:
            return
        i = self.queue[0]
        app = self.apps[i]
        remaining = self.deadline - time.monotonic()
        if remaining > 0:
            self.progress.emit(i, f"NEXT ▸ {app['name']}  {math.ceil(remaining)}s")
            return
        # UAC ダイアログ中にメッセージループが回り _tick が再入しないよう、先に止めてから起動
        self.timer.stop()
        self.queue.pop(0)
        running, _ = winproc.app_state(app, winproc.snapshot())
        if not running:
            ok, err = winproc.launch(app)
            if ok:
                place_on_monitor(app, self)
            else:
                self.errors.append(f"{app['name']} ({err})")
        self._arm()
