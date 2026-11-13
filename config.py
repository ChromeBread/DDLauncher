"""設定の読み書き。%APPDATA%\\CBApps\\DDLauncher\\config.json に保存する。

旧バージョンの保存場所 %APPDATA%\\DDLauncher\\ に設定があれば、初回だけ新しい場所へコピーして引き継ぐ。
旧フォルダは消さない（旧バージョンに戻したときもそのまま動くように）。

構造:
  {
    theme, animation, obs:{...},
    launchers: [ {id, corner, screen, margin, mode, apps:[...]}, ... ]   # 複数ランチャー前提
  }
"""
import copy
import json
import os
import re
import shutil

APP_NAME = "DDLauncher"
VENDOR_DIR = "CBApps"   # 全ソフト共通の親フォルダ
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
CONFIG_DIR = os.path.join(_APPDATA, VENDOR_DIR, APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_DIR = os.path.join(_APPDATA, APP_NAME)   # 旧バージョンの保存場所

CORNERS = ["top_left", "top_right", "bottom_left", "bottom_right"]
THEMES = ["opaque", "transparent"]
COLOR_MODES = ["default", "custom"]
ANIMATIONS = ["full", "reduced", "off"]
LAYOUTS = ["linear", "group"]
AFTER_SEQUENCE = ["none", "minimize", "hide"]
IP_MODES = ["", "static", "dynamic", "mixed"]   # mixed = 有線と Wi-Fi などで固定と自動が混在
LANGUAGES = ["en", "ja", "ko", "id", "fr", "zh_TW"]   # i18n.LANGS と同じ並び
DND_KINDS = ["image", "video", "audio"]   # D&D で読み込むファイルの種類
DEFAULT_EXTS = {   # 種類ごとの既定の拡張子（「種類を選ぶ」がオフのときは常にこれ）
    "image": [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".psd"],
    "video": [".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v"],
    "audio": [".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"],
}
ADD_MEDIA_START = ["play", "first", "stop"]
ADD_MONITOR = ["none", "monitor", "both"]
ADD_NAME = ["stem", "name"]
ADD_CONFLICT = ["number", "ask"]
DND_RESIZE = ["corners", "edges"]   # D&D スペースの大きさを変えるつまみ: 四隅だけ / 四隅と四辺
COLLAPSE = ["hover", "always"]   # hover = 普段はアイコン 1 個、マウスを乗せると展開 / always = 常に展開

DEFAULT_APP = {
    "name": "",
    "path": "",           # exe / ショートカット / URL（ShellExecute に渡す）
    "icon": "",           # カスタムアイコン画像。空なら exe / サイトのアイコン
    "args": "",
    "workdir": "",        # 空なら exe のフォルダ（OBS はこれが無いと起動に失敗する）
    "process_name": "",   # 起動判定に使う exe 名。空なら path から推定
    "delay_sec": 0.0,     # 前のアプリ起動から何秒後に起動するか
    "minimized": False,
    "admin": False,
    "group": "",          # カテゴリ。同じカテゴリは 1 枠に重なり、マウスオーバーで横に出る
    "monitor": "",        # 起動後にウィンドウを移すモニタ（\\.\DISPLAY2 等）。空 = 指定なし
    "autostart": True,    # 起動シーケンスに含めるか
    "obs_guard": True,    # OBS が起動している間は、クリックで開かない（配信中の押し間違い防止）。右クリックメニューからは開ける
    "browser": "",        # path が Web アドレスのとき開くブラウザの exe。空 = Windows の既定のブラウザ
}

DEFAULT_LAUNCHER = {
    "id": "main",
    "corner": "bottom_right",
    "screen": 0,
    "margin": 6,
    "mode": "normal",     # normal / dnd
    "dnd_flush": True,    # D&D モード中は余白を無視して画面端にベタ付け
    "on_top": True,       # ランチャーを最前面に表示
    "dnd_on_top": True,   # D&D スペースを最前面に表示
    "dnd_size": [0, 0],   # D&D スペースの大きさ（倍率 1 のときの px）。0 = 既定の大きさ
    "dnd_pos": None,      # ドラッグで動かした D&D スペースの左上（画面座標）。None = ランチャーの隣
    "dnd_on_launcher": True,   # D&D モード中は、ランチャーの上へのドロップも D&D スペースと同じに扱う
    "dnd_resize": "corners",   # D&D スペースの大きさを変えるつまみ（DND_RESIZE）
    "dnd_warn_count": 5,       # この数以上のファイルをまとめてドロップしたら確認する（0 = 確認しない）
    "dnd_pick": False,         # フォルダや複数の種類をドロップしたら種類を選ぶ / dnd_exts を使う / フォルダの中も読む
    "dnd_exts": DEFAULT_EXTS,  # 種類ごとに読み込む拡張子（dnd_pick がオンのときだけ使う）
    "mini_icon": "",      # 畳んだときのアイコン。空 = DDLauncher の DD / アプリの path = そのアプリのアイコン
    "collapse": "hover",  # hover = アイコン 1 個に畳み、マウスオーバーで展開 / always = 常に展開
    "layout": "group",    # linear = 角で折れる L 字 / group = 角から縦一列
    "max_cols": 4,        # LINEAR: 横の上限（0 = 無制限）
    "max_rows": 0,        # LINEAR: 縦の上限（0 = 無制限。入り切らなければ内側に L 字を足す）
    "group_max_rows": 0,  # GROUP: 縦の上限（0 = 無制限 = 縦一列。超えた分は内側に次の列）
    "group_max_cols": 0,  # GROUP: 横の上限（0 = 無制限。列が上限を超えるなら縦を伸ばす）
    "show_seq_button": False,
    "name_upper": True,   # タイルの表示名をすべて大文字にする（False = 入力したまま）
    "wheel_rotate": True,   # ホイール（Shift+ホイール）でアイコンを回す。False でも ▲▼ と Ctrl+ホイールは使える
    "keep_rotation": False,   # 回した並びを保存するか
    "rot": 0,             # 縦（L 字）の輪をいくつ回したか
    "spins": {},          # カテゴリ → 段の中をいくつ回したか
    "after_sequence": "none",   # シーケンス完了確認後: none / minimize（タスクバー）/ hide（トレイ）
    "scale": 1.0,         # 表示倍率 0.5〜2.5（Ctrl+ホイールでも変更可）
    # 初回起動の見本は OBS だけ（標準のインストール先ならそのまま動く）。言語に依存しない表記にする
    "apps": [
        {"name": "OBS", "path": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
         "admin": False, "delay_sec": 0},   # 初めての人にいきなり UAC を出さない
    ],
}

DEFAULTS = {
    "version": 1,
    "debug": False,       # Debug Mode（保存場所・バックアップ・アンインストール案内などを表示）
    "language": "auto",   # auto = OBS の言語（OBS が無ければ Windows の言語）。対応外は English
    "theme": "opaque",    # 旧版用（opacity から書く）。今の版は opacity だけを見る
    "opacity": 100,       # ランチャーの背景の不透明度（%）
    "color_mode": "default",   # custom = colors の色を使う（default に戻しても colors は消さない）
    "colors": {},         # theme.CUSTOM_KEYS → "#rrggbb"。変えた色だけ入る
    "animation": "full",
    "obs": {"host": "localhost", "port": 4455, "password": "", "target_scene": "", "add": {
        # D&D で OBS に追加するソースの初期状態（既定は今までと同じ: 表示・すぐ再生・0 dB・ファイル名）
        "visible": True,         # シーン上で表示するか
        "media_start": "play",   # 映像 / 音: play = すぐ再生 / first = 先頭の 1 個だけ再生 / stop = 停止しておく
        "volume_db": 0.0,        # 映像 / 音の音量（OBS の音声ミキサーと同じ dB）
        "mute": False,           # ミュートして追加
        "loop": False,           # ループ再生
        "loop_exts": [],         # ループする拡張子（空 = 映像 / 音のすべて）
        "restart": True,         # ソースが表示されたら最初から再生（OBS の既定と同じ）
        "monitor": "none",       # 音声モニタリング: none / monitor（モニターのみ）/ both（モニターと出力）
        "fit": False,            # 画像 / 映像を画面の大きさに合わせる
        "name": "stem",          # ソース名: stem = ファイル名（拡張子なし）/ name = ファイル名（拡張子あり）
        "on_conflict": "number", # 名前が被ったとき: number = 自動で連番 / ask = どうするか聞く
    }},
    # ポート管理: 使うポートを登録しておき、想定外のプロセスが使っていたら衝突として表示
    "ports": [{"port": 4455, "memo": "OBS WebSocket", "process": "obs64.exe"}],
    "ip_mode": "",        # この PC の IP アドレスが固定か可変かのメモ（"" / static / dynamic）。IP そのものは取得しない
    "ui": {"app_col_widths": [], "port_col_widths": [], "app_col_order": [], "app_split": []},   # 設定画面の列幅・列の並び・表とグラフの境目
    # サウンドデバイスのプロファイル（中身は sound.py の説明を参照）。active = 最後に適用したもの
    "sound": {"profiles": [], "active": "", "auto_restore": False, "with_levels": True, "spr_dir": ""},
    "launchers": [DEFAULT_LAUNCHER],
}


def _fill(data: dict, defaults: dict) -> dict:
    """defaults に無いキーを補完（ユーザー値は保持）。"""
    out = copy.deepcopy(defaults)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fill(v, out[k])
        else:
            out[k] = v
    return out


def parse_exts(value) -> list[str]:
    """「png, .JPG  webp」や [".png", "jpg"] → [".png", ".jpg", ".webp"]。拡張子として使えない文字のものは捨てる。"""
    items = re.split(r"[\s,;、，；]+", value) if isinstance(value, str) else [str(v) for v in value]
    out = []
    for item in items:
        e = "." + item.strip().lstrip(".").lower()
        if re.fullmatch(r"\.[a-z0-9]{1,10}", e) and e not in out:
            out.append(e)
    return out[:50]


def normalize(cfg: dict) -> dict:
    if "opacity" not in cfg:   # 旧版の「半透明」テーマは不透明度 60 % として引き継ぐ
        cfg = dict(cfg, opacity=60 if cfg.get("theme") == "transparent" else 100)
    cfg = _fill(cfg, {k: v for k, v in DEFAULTS.items() if k != "launchers"})
    ports = []
    for p in cfg["ports"]:
        try:
            port = int(p.get("port", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.append({"port": port, "memo": str(p.get("memo", "")), "process": str(p.get("process", ""))})
    cfg["ports"] = ports
    if cfg["ip_mode"] not in IP_MODES:
        cfg["ip_mode"] = ""
    launchers = cfg.get("launchers") or copy.deepcopy(DEFAULTS["launchers"])
    cfg["launchers"] = []
    for l in launchers:
        l = _fill(l, {k: v for k, v in DEFAULT_LAUNCHER.items() if k != "apps"})
        l["apps"] = [_fill(a, DEFAULT_APP) for a in l.get("apps", [])]
        if l["corner"] not in CORNERS:
            l["corner"] = "bottom_right"
        try:
            l["scale"] = min(max(float(l["scale"]), 0.5), 2.5)
        except (TypeError, ValueError):
            l["scale"] = 1.0
        for key in ("max_cols", "max_rows", "group_max_cols", "group_max_rows"):
            try:
                l[key] = min(max(int(l[key]), 0), 50)
            except (TypeError, ValueError):
                l[key] = DEFAULT_LAUNCHER[key]
        l["keep_rotation"] = bool(l["keep_rotation"])
        l["on_top"], l["dnd_on_top"] = bool(l["on_top"]), bool(l["dnd_on_top"])
        l["dnd_on_launcher"] = bool(l["dnd_on_launcher"])
        try:
            l["dnd_warn_count"] = min(max(int(l["dnd_warn_count"]), 0), 999)
        except (TypeError, ValueError):
            l["dnd_warn_count"] = DEFAULT_LAUNCHER["dnd_warn_count"]
        l["dnd_pick"] = bool(l["dnd_pick"])
        exts = l["dnd_exts"] if isinstance(l["dnd_exts"], dict) else {}
        l["dnd_exts"] = {k: parse_exts(exts[k]) if isinstance(exts.get(k), (list, str)) else list(DEFAULT_EXTS[k])
                         for k in DND_KINDS}
        if l["dnd_resize"] not in DND_RESIZE:
            l["dnd_resize"] = DEFAULT_LAUNCHER["dnd_resize"]
        try:
            w, h = (int(v) for v in l["dnd_size"])
            l["dnd_size"] = [min(max(w, 0), 4000), min(max(h, 0), 4000)]
        except (TypeError, ValueError):
            l["dnd_size"] = [0, 0]
        try:
            l["dnd_pos"] = [int(v) for v in l["dnd_pos"]] if l["dnd_pos"] is not None else None
            if len(l["dnd_pos"] or [0, 0]) != 2:
                l["dnd_pos"] = None
        except (TypeError, ValueError):
            l["dnd_pos"] = None
        l["wheel_rotate"] = bool(l["wheel_rotate"])
        try:
            l["rot"] = int(l["rot"])
            l["spins"] = {str(g): int(n) for g, n in dict(l["spins"]).items()}
        except (TypeError, ValueError):
            l["rot"], l["spins"] = 0, {}
        if not isinstance(l["mini_icon"], str):
            l["mini_icon"] = ""
        if l["collapse"] not in COLLAPSE:
            l["collapse"] = "hover"
        if l["layout"] not in LAYOUTS or l["layout"] == "linear":   # shortcut: LINEAR は調整中なので GROUP に固定。完成したら "linear" の判定を外す
            l["layout"] = "group"
        if l["after_sequence"] not in AFTER_SEQUENCE:
            l["after_sequence"] = "none"
        cfg["launchers"].append(l)
    cfg["debug"] = bool(cfg["debug"])
    if cfg["language"] != "auto" and cfg["language"] not in LANGUAGES:
        cfg["language"] = "auto"
    if cfg["theme"] not in THEMES:
        cfg["theme"] = "opaque"
    snd = cfg["sound"] if isinstance(cfg["sound"], dict) else {}
    snd = _fill(snd, DEFAULTS["sound"])
    snd["profiles"] = [p for p in snd["profiles"] if isinstance(p, dict) and isinstance(p.get("name"), str)]         if isinstance(snd["profiles"], list) else []
    snd["active"], snd["spr_dir"] = str(snd["active"]), str(snd["spr_dir"])
    snd["auto_restore"], snd["with_levels"] = bool(snd["auto_restore"]), bool(snd["with_levels"])
    cfg["sound"] = snd
    try:
        cfg["opacity"] = min(max(int(cfg["opacity"]), 20), 100)
    except (TypeError, ValueError):
        cfg["opacity"] = 100
    if cfg["color_mode"] not in COLOR_MODES:
        cfg["color_mode"] = "default"
    colors = cfg["colors"] if isinstance(cfg["colors"], dict) else {}
    cfg["colors"] = {str(k): v.lower() for k, v in colors.items() if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)}
    if cfg["animation"] not in ANIMATIONS:
        cfg["animation"] = "full"
    add, default = cfg["obs"]["add"] if isinstance(cfg["obs"].get("add"), dict) else {}, DEFAULTS["obs"]["add"]
    for key in ("visible", "mute", "loop", "restart", "fit"):
        add[key] = bool(add.get(key, default[key]))
    for key, allowed in (("media_start", ADD_MEDIA_START), ("monitor", ADD_MONITOR), ("name", ADD_NAME),
                         ("on_conflict", ADD_CONFLICT)):
        if add.get(key) not in allowed:
            add[key] = default[key]
    add["loop_exts"] = parse_exts(add["loop_exts"]) if isinstance(add.get("loop_exts"), (list, str)) else []
    try:
        add["volume_db"] = round(min(max(float(add["volume_db"]), -100.0), 0.0), 1)
    except (KeyError, TypeError, ValueError):
        add["volume_db"] = default["volume_db"]
    cfg["obs"]["add"] = add
    return cfg


def migrate_legacy(legacy_dir: str = LEGACY_DIR, config_dir: str = CONFIG_DIR) -> bool:
    """新しい場所に設定が無く、旧い場所にあればコピーする。コピーしたら True。

    コピーなので旧フォルダはそのまま残る。途中で落ちても中途半端な config.json が残らないよう、
    一時ファイルに書いてから置き換える。サイトのアイコン（favicons）も取り直さずに済むよう一緒にコピーする。
    """
    new_path = os.path.join(config_dir, "config.json")
    old_path = os.path.join(legacy_dir, "config.json")
    if os.path.exists(new_path) or not os.path.isfile(old_path):
        return False
    os.makedirs(config_dir, exist_ok=True)
    old_icons = os.path.join(legacy_dir, "favicons")
    if os.path.isdir(old_icons):
        try:
            shutil.copytree(old_icons, os.path.join(config_dir, "favicons"), dirs_exist_ok=True)
        except OSError:
            pass   # アイコンは無くても次回取り直せる
    tmp = new_path + ".tmp"
    shutil.copyfile(old_path, tmp)
    os.replace(tmp, new_path)
    return True


def load() -> dict:
    try:
        migrate_legacy()
    except OSError:
        pass   # コピーできなければ既定値で起動する（旧フォルダは残っているので失われない）
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        return normalize({})
    except (OSError, ValueError):
        # 壊れた設定は退避してから既定値で起動（上書きで失わないように）
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".broken")
        except OSError:
            pass
        return normalize({})


def save(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)  # 途中で落ちても既存設定が壊れない


# ---- エクスポート / インポート / バックアップ ----
# 項目ごとに書き出せるよう、設定のキーをまとめて扱う。ファイルは人が読める JSON（字下げ付き、日本語もそのまま）
SECTIONS = {
    "launchers": ["launchers"],                                   # ランチャーとアプリ
    "ports": ["ports", "ip_mode"],                                # ポート管理
    "general": ["language", "theme", "opacity", "color_mode", "colors", "animation", "ui", "debug"],  # 全般
    "obs": ["obs"],                                               # OBS 接続（パスワードを含む）
    "sound": ["sound"],                                           # サウンドデバイスのプロファイル
}
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
BACKUP_KEEP = 5   # 自動バックアップは直近 5 個まで


def export_data(cfg: dict, sections: list[str]) -> dict:
    out = {"app": APP_NAME, "version": cfg.get("version", 1), "sections": [s for s in SECTIONS if s in sections]}
    for s in out["sections"]:
        for key in SECTIONS[s]:
            out[key] = copy.deepcopy(cfg[key])
    return out


def write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def import_data(cfg: dict, data: dict) -> tuple[dict, list[str]]:
    """data に入っている項目だけ cfg に上書きした新しい設定と、上書きした項目名を返す。

    エクスポートしたファイルのほか、config.json をそのまま渡しても読める（入っている項目を全部使う）。
    """
    if not isinstance(data, dict):
        return cfg, []
    wanted = data.get("sections") if isinstance(data.get("sections"), list) else list(SECTIONS)
    out = copy.deepcopy(cfg)
    found = []
    for s in SECTIONS:
        keys = [k for k in SECTIONS[s] if k in data]
        if s in wanted and keys:
            for k in keys:
                out[k] = copy.deepcopy(data[k])
            found.append(s)
    return normalize(out), found


def backup_current(backup_dir: str = BACKUP_DIR, config_path: str = CONFIG_PATH, keep: int = BACKUP_KEEP) -> str | None:
    """今の config.json を backups フォルダの config-日時.json にコピーし、古いものは keep 個を残してゴミ箱へ移す。"""
    import time
    if not os.path.isfile(config_path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, time.strftime("config-%Y%m%d-%H%M%S.json"))
    shutil.copyfile(config_path, dst)
    # 自分で作ったバックアップ（config-*.json）だけを対象にする。ほかのファイルには触らない
    mine = sorted(f for f in os.listdir(backup_dir) if f.startswith("config-") and f.endswith(".json"))
    for old in mine[:-keep] if keep > 0 else []:
        move_to_trash(os.path.join(backup_dir, old))
    return dst


def move_to_trash(path: str) -> bool:
    """完全には消さず、Windows のゴミ箱へ移す。失敗したら何もしない（消えたままにはしない）。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,   # pFrom は \0\0 終端
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    try:
        return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)
    except (AttributeError, OSError):
        return False
