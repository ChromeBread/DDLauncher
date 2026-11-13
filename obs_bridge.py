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

from config import DEFAULT_EXTS
from i18n import tr

IMAGE_EXT = set(DEFAULT_EXTS["image"])
# OBS 未起動のあいだ 2 秒ごとの接続失敗をスタックトレース付きで出し続けるため黙らせる（失敗は呼び出し側で扱う）
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
VIDEO_EXT = set(DEFAULT_EXTS["video"])
AUDIO_EXT = set(DEFAULT_EXTS["audio"])
SCAN_LIMIT, FILE_LIMIT = 20000, 1000   # フォルダの中を見る上限（固まらないように）/ 読み込む候補の上限


def file_kind(path: str, exts: dict | None = None) -> str | None:
    """"image" / "video" / "audio" / None。exts = {種類: [拡張子]}（None なら既定の拡張子）。"""
    ext = os.path.splitext(path)[1].lower()
    return next((kind for kind, allowed in (exts or DEFAULT_EXTS).items() if ext in allowed), None)


def source_spec(path: str, exts: dict | None = None) -> tuple[str, dict] | None:
    """ファイル → (inputKind, inputSettings)。非対応なら None。"""
    kind = file_kind(path, exts)
    if kind == "image":
        return "image_source", {"file": path}
    if kind in ("video", "audio"):
        return "ffmpeg_source", {"local_file": path, "is_local_file": True, "looping": False}
    return None


def collect_files(paths: list[str], exts: dict | None = None) -> tuple[list[str], bool]:
    """ドロップされたファイルとフォルダ（中のサブフォルダも）から、読み込めるファイルを集める。

    (ファイル, 上限で打ち切ったか)。大きなフォルダでも固まらないよう、見る数と集める数に上限を設ける。
    """
    out, scanned = [], 0
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    scanned += 1
                    fp = os.path.join(root, f)
                    if file_kind(fp, exts):
                        out.append(fp)
                    if len(out) >= FILE_LIMIT or scanned >= SCAN_LIMIT:
                        return out, True
        elif file_kind(p, exts):
            out.append(p)
            if len(out) >= FILE_LIMIT:
                return out, True
    return out, False


def unique_name(base: str, existing: set[str]) -> str:
    """OBS の入力名は全体で一意である必要があるため連番を付ける。"""
    name, n = base, 2
    while name in existing:
        name, n = f"{base} ({n})", n + 1
    return name


MONITOR_TYPES = {"monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY", "both": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"}


def apply_media_options(client, name: str, opt: dict, first: bool) -> None:
    """映像 / 音のソースを作った直後に、再生 / 停止・音量・ミュート・モニタリングを設定する。"""
    start = opt.get("media_start", "play")
    if start == "stop" or (start == "first" and not first):
        client.trigger_media_input_action(name, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
    if opt.get("volume_db", 0.0):
        client.set_input_volume(name, vol_db=float(opt["volume_db"]))
    if opt.get("mute"):
        client.set_input_mute(name, True)
    if opt.get("monitor") in MONITOR_TYPES:
        client.set_input_audio_monitor_type(name, MONITOR_TYPES[opt["monitor"]])


def loops(path: str, opt: dict) -> bool:
    """ループするか。loop_exts が空なら映像 / 音のすべて、指定があればその拡張子だけ。"""
    allowed = opt.get("loop_exts") or []
    return bool(opt.get("loop")) and (not allowed or os.path.splitext(path)[1].lower() in allowed)


def fit_transform(width: int, height: int) -> dict:
    """画面（OBS のキャンバス）いっぱいに、縦横比を保って収める配置。"""
    return {"positionX": 0.0, "positionY": 0.0, "alignment": 5, "rotation": 0.0,   # 5 = 左上
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0, "boundsWidth": float(width), "boundsHeight": float(height)}


def base_name(path: str, opt: dict) -> str:
    """ソース名の元（設定でファイル名の拡張子あり / なし）。"""
    name = os.path.basename(path)
    return name if opt.get("name") == "name" else os.path.splitext(name)[0]


def taken_names(client) -> tuple[set[str], set[str]]:
    """(ソースの名前, 使用済みの名前)。OBS ではシーンとグループもソースと同じ名前の枠を使うので、使用済みに含める。"""
    inputs = {i["inputName"] for i in client.get_input_list().inputs}
    taken = inputs | {s["sceneName"] for s in client.get_scene_list().scenes} | set(client.get_group_list().groups)
    return inputs, taken


def name_conflicts(paths: list[str], opt: dict, taken: set[str]) -> list[tuple[str, str, str]]:
    """名前が被るファイル: [(パス, 元の名前, 連番を付けた案)]。同じドロップの中で被るもの（2 個目以降）も含む。"""
    used, out = set(taken), []
    for p in paths:
        base = base_name(p, opt)
        if base in used:
            suggestion = unique_name(base, used)
            out.append((p, base, suggestion))
            used.add(suggestion)
        else:
            used.add(base)
    return out


class ObsBridge(QObject):
    finished = Signal(bool, str)  # (成功, メッセージ) — ワーカースレッドから emit、Qt がメインスレッドへ配送
    names_ready = Signal(object)  # check_names の結果: (paths, exts, ソースの名前, 使用済みの名前)

    def __init__(self, obs_cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = obs_cfg

    def _connect(self):
        return obs.ReqClient(host=self.cfg["host"], port=int(self.cfg["port"]), password=self.cfg["password"], timeout=3)

    def check_names(self, paths: list[str], exts: dict | None = None) -> None:
        """追加する前に OBS の使用済みの名前を取りに行く（名前が被ったらどうするか聞くため）。"""
        threading.Thread(target=self._names_worker, args=(list(paths), exts), daemon=True).start()

    def _names_worker(self, paths: list[str], exts: dict | None) -> None:
        try:
            client = self._connect()
        except Exception as e:
            self.finished.emit(False, tr("obs.connect_failed", e=e))
            return
        try:
            self.names_ready.emit((paths, exts, *taken_names(client)))
        except Exception as e:
            self.finished.emit(False, tr("obs.error", e=e))
        finally:
            client.disconnect()

    def add_files(self, paths: list[str], exts: dict | None = None, plan: dict | None = None) -> None:
        """plan = {パス: ("new", 名前) / ("reuse", 既存のソース名) / ("skip", None)}。無いファイルはファイル名で追加。"""
        # 接続待ちで UI を固めないよう別スレッドで実行
        threading.Thread(target=self._worker, args=(list(paths), exts, plan), daemon=True).start()

    def _worker(self, paths: list[str], exts: dict | None = None, plan: dict | None = None) -> None:
        try:
            client = self._connect()
        except Exception as e:
            self.finished.emit(False, tr("obs.connect_failed", e=e))
            return
        try:
            scene = self.cfg.get("target_scene") or \
                client.get_current_program_scene().current_program_scene_name
            existing = taken_names(client)[1]
            added, failed = 0, []
            opt = self.cfg.get("add") or {}
            canvas = client.get_video_settings() if opt.get("fit") else None
            first_media = True
            for p in paths:
                spec = source_spec(p, exts)
                if not spec:
                    continue
                kind, settings = spec
                action, wanted = (plan or {}).get(p, ("new", None))
                if action == "skip":
                    continue
                if action == "reuse":   # 既存のソースをこのシーンにもう 1 つ配置する（音量などはそのソースの設定のまま）
                    try:
                        item = client.create_scene_item(scene, wanted, bool(opt.get("visible", True)))
                        added += 1
                        if canvas and file_kind(p, exts) != "audio":
                            client.set_scene_item_transform(scene, item.scene_item_id, fit_transform(canvas.base_width, canvas.base_height))
                    except Exception as e:
                        failed.append(e)
                    continue
                media = kind == "ffmpeg_source"
                if media:
                    settings.update(looping=loops(p, opt), restart_on_activate=bool(opt.get("restart", True)))
                name = unique_name(wanted or base_name(p, opt), existing)   # 聞いている間に OBS 側で埋まっても被らないように
                try:   # 1 個失敗しても残りのファイルは続けて追加する
                    item = client.create_input(scene, name, kind, settings, bool(opt.get("visible", True)))
                except Exception as e:
                    failed.append(e)
                    continue
                existing.add(name)
                added += 1
                try:
                    if media:
                        apply_media_options(client, name, opt, first_media)
                        first_media = False
                    if canvas and file_kind(p, exts) != "audio":   # 音だけのファイルは画面に出ないので合わせない
                        client.set_scene_item_transform(scene, item.scene_item_id, fit_transform(canvas.base_width, canvas.base_height))
                except Exception as e:
                    failed.append(e)
            if failed:
                self.finished.emit(False, tr("obs.added_partial", scene=scene, n=added, m=len(failed), e=failed[0]))
            else:
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
