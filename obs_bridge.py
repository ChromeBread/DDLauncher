"""obs-websocket (v5) 経由でファイルをソースとして追加する。

通常権限のランチャー → localhost の WebSocket → 管理者権限の OBS。
ウィンドウメッセージ（D&D）と違い UIPI に遮断されないので、権限差があっても安全に届く。
"""
import logging
import os
import threading
import time

import obsws_python as obs
from PySide6.QtCore import QObject, Signal

from i18n import tr

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".psd"}
# OBS 未起動のあいだ 2 秒ごとの接続失敗をスタックトレース付きで出し続けるため黙らせる（失敗は呼び出し側で扱う）
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}


def source_spec(path: str) -> tuple[str, dict] | None:
    """ファイル → (inputKind, inputSettings)。非対応なら None。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image_source", {"file": path}
    if ext in VIDEO_EXT or ext in AUDIO_EXT:
        return "ffmpeg_source", {"local_file": path, "is_local_file": True, "looping": False}
    return None


def unique_name(base: str, existing: set[str]) -> str:
    """OBS の入力名は全体で一意である必要があるため連番を付ける。"""
    name, n = base, 2
    while name in existing:
        name, n = f"{base} ({n})", n + 1
    return name


class ObsBridge(QObject):
    finished = Signal(bool, str)  # (成功, メッセージ) — ワーカースレッドから emit、Qt がメインスレッドへ配送

    def __init__(self, obs_cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = obs_cfg

    def add_files(self, paths: list[str]) -> None:
        # 接続待ちで UI を固めないよう別スレッドで実行
        threading.Thread(target=self._worker, args=(list(paths),), daemon=True).start()

    def _worker(self, paths: list[str]) -> None:
        try:
            client = obs.ReqClient(host=self.cfg["host"], port=int(self.cfg["port"]),
                                   password=self.cfg["password"], timeout=3)
        except Exception as e:
            self.finished.emit(False, tr("obs.connect_failed", e=e))
            return
        try:
            scene = self.cfg.get("target_scene") or \
                client.get_current_program_scene().current_program_scene_name
            existing = {i["inputName"] for i in client.get_input_list().inputs}
            added = 0
            for p in paths:
                spec = source_spec(p)
                if not spec:
                    continue
                kind, settings = spec
                name = unique_name(os.path.splitext(os.path.basename(p))[0], existing)
                client.create_input(scene, name, kind, settings, True)
                existing.add(name)
                added += 1
            self.finished.emit(True, tr("obs.added", scene=scene, n=added))
        except Exception as e:
            self.finished.emit(False, tr("obs.error", e=e))
        finally:
            client.disconnect()


def fmt_bytes(n: int) -> str:
    """タイル中段（アイコンの右下）に出す通信量。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


class ObsStatus(QObject):
    """OBS の配信 / 録画状態を 2 秒ごとに取りに行く。updated: {"stream", "record", "bytes"} / 未接続なら None。"""
    updated = Signal(object)

    def __init__(self, get_cfg, parent=None):
        super().__init__(parent)
        self.get_cfg = get_cfg   # 設定画面で接続先が変わっても次の再接続で拾う
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        client = None
        while True:
            try:
                if client is None:
                    c = self.get_cfg()
                    client = obs.ReqClient(host=c["host"], port=int(c["port"]), password=c["password"], timeout=3)
                st, rec = client.get_stream_status(), client.get_record_status()
                live = st.output_active
                self.updated.emit({"stream": live, "record": rec.output_active,
                                   "bytes": st.output_bytes if live else rec.output_bytes})
            except Exception:   # OBS 未起動 / 切断 / 認証失敗: 次の周期で接続し直す
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                client = None
                self.updated.emit(None)
            time.sleep(2)
