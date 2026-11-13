"""Windows のスタートアップ項目（ログオン時に起動するアプリ）の読み書き。

対象:
  - HKCU\\...\\Run と ユーザーのスタートアップフォルダ ... 編集できる（管理者不要）
  - HKLM\\...\\Run（32 ビット版も）と 全ユーザー共通のスタートアップフォルダ ... 表示だけ（変更には管理者権限が要る）

有効 / 無効は、タスクマネージャーと同じ StartupApproved の値で切り替える（登録そのものは消さない）。
変更する前に backup() で今の登録を .reg とファイルのコピーに残す。
"""
import os
import shutil
import struct
import subprocess
import time
import winreg

import config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32_KEY = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
SELF_NAME = "DDLauncher"   # winproc.RUN_NAME と同じ。自分の登録は「全般」のスタートアップで変える

USER_FOLDER = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
COMMON_FOLDER = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs\StartUp")

BACKUP_DIR = os.path.join(config.CONFIG_DIR, "startup_backups")
BACKUP_KEEP = 5
LIST_LIMIT = 300   # 1 か所あたりの表示上限（表がはみ出したり重くなったりしないように）

# source: (ルートキー, Run のキー or None, StartupApproved のサブキー, フォルダ or None, 編集できるか)
SOURCES = {
    "hkcu_run": (winreg.HKEY_CURRENT_USER, RUN_KEY, "Run", None, True),
    "user_folder": (winreg.HKEY_CURRENT_USER, None, "StartupFolder", USER_FOLDER, True),
    "hklm_run": (winreg.HKEY_LOCAL_MACHINE, RUN_KEY, "Run", None, False),
    "hklm_run32": (winreg.HKEY_LOCAL_MACHINE, RUN32_KEY, "Run32", None, False),
    "common_folder": (winreg.HKEY_LOCAL_MACHINE, None, "StartupFolder", COMMON_FOLDER, False),
}


# ---- 有効 / 無効（StartupApproved） ----

def approved_enabled(data) -> bool:
    """StartupApproved の値から有効かどうか。先頭が偶数（02, 06）なら有効、奇数（03）なら無効。値が無ければ有効。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return True
    return data[0] % 2 == 0


def approved_value(enabled: bool) -> bytes:
    """タスクマネージャーと同じ 12 バイト: 状態(4) + 無効にした日時 FILETIME(8)。"""
    if enabled:
        return struct.pack("<IQ", 2, 0)
    filetime = int((time.time() + 11644473600) * 10_000_000)   # 1601-01-01 からの 100ns 単位
    return struct.pack("<IQ", 3, filetime)


def _read_values(root, path: str) -> dict:
    """{名前: (値, 型)}。キーが無ければ空。"""
    out = {}
    try:
        with winreg.OpenKey(root, path) as k:
            i = 0
            while i < 10_000:   # 念のための上限
                try:
                    name, value, kind = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = (value, kind)
                i += 1
    except OSError:
        pass
    return out


# ---- ショートカット（.lnk）の読み取り ----

def read_lnk(path: str) -> tuple[str, str]:
    """ショートカットのリンク先と引数を読む（MS-SHLLINK 形式）。読めなければ ("", "")。"""
    try:
        with open(path, "rb") as f:
            return parse_lnk(f.read(1 << 20))
    except OSError:
        return "", ""


def parse_lnk(b: bytes) -> tuple[str, str]:
    try:
        if len(b) < 0x4C or struct.unpack_from("<I", b, 0)[0] != 0x4C:
            return "", ""
        flags = struct.unpack_from("<I", b, 0x14)[0]
        pos = 0x4C
        if flags & 0x1:   # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", b, pos)[0]
        target = ""
        if flags & 0x2:   # HasLinkInfo
            info = pos
            size, hsize, iflags, _vol, base_off, _net, suffix_off = struct.unpack_from("<7I", b, info)
            if iflags & 0x1:
                if hsize >= 0x24:   # Unicode 版のパスがある
                    ubase, usuffix = struct.unpack_from("<2I", b, info + 28)
                    target = _wstr(b, info + ubase) + _wstr(b, info + usuffix)
                if not target:
                    target = _astr(b, info + base_off) + _astr(b, info + suffix_off)
            pos += size
        unicode = bool(flags & 0x80)
        strings = {}
        for bit, key in ((0x4, "name"), (0x8, "rel"), (0x10, "workdir"), (0x20, "args"), (0x40, "icon")):
            if flags & bit:
                n = struct.unpack_from("<H", b, pos)[0]
                pos += 2
                width = 2 if unicode else 1
                raw = b[pos:pos + n * width]
                strings[key] = raw.decode("utf-16-le" if unicode else "mbcs", "replace")
                pos += n * width
        return target, strings.get("args", "")
    except (struct.error, IndexError, LookupError):
        return "", ""


def _wstr(b: bytes, pos: int) -> str:
    end = pos
    while end + 1 < len(b) and b[end:end + 2] != b"\0\0":
        end += 2
    return b[pos:end].decode("utf-16-le", "replace")


def _astr(b: bytes, pos: int) -> str:
    end = b.find(b"\0", pos)
    return b[pos:end if end >= 0 else len(b)].decode("mbcs", "replace")


# ---- 一覧 ----

def list_items() -> list[dict]:
    """スタートアップ項目の一覧。編集できるもの → 表示だけのもの の順。

    各項目: source, name, command, enabled, editable, file（フォルダの項目のファイルパス）, ext, reg_kind
    """
    items = []
    for source, (root, run, approved_sub, folder, editable) in SOURCES.items():
        approved = _read_values(root, APPROVED_KEY + "\\" + approved_sub)
        found = []
        if run:
            for name, (value, kind) in _read_values(root, run).items():
                if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not name:
                    continue
                found.append(dict(source=source, name=name, command=str(value), reg_kind=kind, file="", ext="",
                                  enabled=approved_enabled(approved.get(name, (None,))[0])))
        elif folder and os.path.isdir(folder):
            for fn in sorted(os.listdir(folder))[:LIST_LIMIT * 2]:
                path = os.path.join(folder, fn)
                if fn.lower() == "desktop.ini" or not os.path.isfile(path):
                    continue
                stem, ext = os.path.splitext(fn)
                command = path
                if ext.lower() == ".lnk":
                    target, args = read_lnk(path)
                    if target:
                        command = join_command(target, args)
                found.append(dict(source=source, name=stem, command=command, reg_kind=None, file=path, ext=ext,
                                  enabled=approved_enabled(approved.get(fn, (None,))[0])))
        for it in found[:LIST_LIMIT]:
            it["editable"] = editable and not (source == "hkcu_run" and it["name"] == SELF_NAME)
            it["self"] = source == "hkcu_run" and it["name"] == SELF_NAME
            items.append(it)
    return items


def join_command(target: str, args: str) -> str:
    t = f'"{target}"' if " " in target and not target.startswith('"') else target
    return f"{t} {args}".strip() if args else t


def split_command(command: str) -> tuple[str, str]:
    """「"C:\\a b\\x.exe" -arg」→ ("C:\\a b\\x.exe", "-arg")。クォートが無ければ最初の空白で分ける。"""
    c = command.strip()
    if c.startswith('"'):
        end = c.find('"', 1)
        if end > 0:
            return c[1:end], c[end + 1:].strip()
        return c.strip('"'), ""
    target, _, args = c.partition(" ")
    return target, args.strip()


def editable_folder_command(item: dict) -> bool:
    """フォルダの項目でコマンドを書き換えられるのはショートカットだけ。"""
    return item.get("ext", "").lower() == ".lnk"


# ---- 場所を開く / プロパティ ----

def resolve_target(command: str) -> str:
    """コマンドから起動するファイルの実際のパス。環境変数を展開し、パスが無ければ PATH から探す。見つからなければ ""。"""
    target, _args = split_command(command)
    target = os.path.expandvars(target.strip())
    if not target or "://" in target:
        return ""   # URL などはファイルではない
    if os.path.exists(target):
        return os.path.abspath(target)
    if not os.path.dirname(target):
        found = shutil.which(target)   # rundll32.exe など、名前だけのもの
        if found:
            return found
    return ""


def reveal(path: str) -> None:
    """エクスプローラーでフォルダを開き、そのファイルを選んだ状態にする。"""
    if path and os.path.exists(path):
        subprocess.Popen(f'explorer /select,"{os.path.abspath(path)}"')   # 引数はこの形でないと選択されない


def show_properties(path: str, hwnd: int = 0) -> None:
    """Windows 標準のプロパティ画面を開く（ショートカットならリンク先も見られる）。"""
    if not path or not os.path.exists(path):
        return
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.shell32.SHObjectProperties
    fn.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
    fn.restype = wintypes.BOOL
    fn(hwnd, 0x2, os.path.abspath(path), None)   # 0x2 = SHOP_FILEPATH


# ---- 変更 ----

def _approved_path(source: str) -> tuple:
    root, _run, sub, _folder, _ed = SOURCES[source]
    return root, APPROVED_KEY + "\\" + sub


def _approved_get(source: str, key_name: str):
    root, path = _approved_path(source)
    return _read_values(root, path).get(key_name, (None, None))[0]


def _approved_set(source: str, key_name: str, data: bytes | None) -> None:
    root, path = _approved_path(source)
    if data is None:
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, key_name)
        except FileNotFoundError:
            pass
        return
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, key_name, 0, winreg.REG_BINARY, data)


def _key_name(item: dict) -> str:
    """StartupApproved で使う名前（Run は値の名前、フォルダはファイル名）。"""
    return os.path.basename(item["file"]) if item["source"] == "user_folder" else item["name"]


def set_enabled(item: dict, enabled: bool) -> None:
    _approved_set(item["source"], _key_name(item), approved_value(enabled))


def add_run(name: str, command: str) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)


def update(item: dict, name: str, command: str) -> dict:
    """名前とコマンドを書き換える。書き換えた後の項目を返す。"""
    new = dict(item, name=name, command=command)
    if item["source"] == "hkcu_run":
        kind = item.get("reg_kind") or winreg.REG_SZ
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, kind, command)
            if name != item["name"]:
                winreg.DeleteValue(k, item["name"])
        if name != item["name"]:
            _move_approved(item, new)
        return new
    if item["source"] == "user_folder":
        if command != item["command"] and editable_folder_command(item):
            target, args = split_command(command)
            _write_lnk(item["file"], target, args)
        if name != item["name"]:
            dst = os.path.join(os.path.dirname(item["file"]), name + item["ext"])
            if os.path.exists(dst):
                raise FileExistsError(dst)
            os.rename(item["file"], dst)
            new["file"] = dst
            _move_approved(item, new)
        return new
    raise PermissionError(item["source"])


def _move_approved(old: dict, new: dict) -> None:
    data = _approved_get(old["source"], _key_name(old))
    if data is not None:
        _approved_set(new["source"], _key_name(new), data)
        _approved_set(old["source"], _key_name(old), None)


def remove(item: dict) -> None:
    """Run の値は消す（backup に残る）。フォルダのファイルはゴミ箱へ移す。"""
    if item["source"] == "hkcu_run":
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, item["name"])
    elif item["source"] == "user_folder":
        if os.path.dirname(os.path.abspath(item["file"])).lower() != os.path.abspath(USER_FOLDER).lower():
            raise PermissionError(item["file"])   # スタートアップフォルダの外は絶対に触らない
        if not config.move_to_trash(item["file"]):
            raise OSError(item["file"])
    else:
        raise PermissionError(item["source"])
    _approved_set(item["source"], _key_name(item), None)


def _write_lnk(path: str, target: str, args: str) -> None:
    """ショートカットのリンク先と引数を書き換える（Windows 標準の WScript.Shell を使う）。"""
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:DDL_LNK);"
              "$s.TargetPath=$env:DDL_TARGET;$s.Arguments=$env:DDL_ARGS;$s.Save()")
    env = dict(os.environ, DDL_LNK=path, DDL_TARGET=target, DDL_ARGS=args)   # パスは環境変数で渡す（クォートの崩れを防ぐ）
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                       capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise OSError(r.stderr.decode("mbcs", "replace").strip() or "powershell failed")


# ---- バックアップ ----

def reg_text(sections: list[tuple[str, dict]]) -> str:
    """[(キーのフルパス, {名前: (値, 型)})] を .reg ファイルの文字列にする（ダブルクリックで書き戻せる）。"""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for key, values in sections:
        lines.append(f"[{key}]")
        for name, (value, kind) in values.items():
            n = '@' if name == "" else '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
            if kind == winreg.REG_SZ:
                v = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
            elif kind == winreg.REG_DWORD:
                v = f"dword:{int(value) & 0xFFFFFFFF:08x}"
            elif kind == winreg.REG_BINARY:
                v = "hex:" + ",".join(f"{x:02x}" for x in bytes(value))
            elif kind == winreg.REG_EXPAND_SZ:
                v = "hex(2):" + ",".join(f"{x:02x}" for x in (str(value) + "\0").encode("utf-16-le"))
            else:
                continue   # スタートアップでは使わない型
            lines.append(f"{n}={v}")
        lines.append("")
    return "\r\n".join(lines) + "\r\n"


def backup(backup_dir: str = BACKUP_DIR, keep: int = BACKUP_KEEP) -> str:
    """ユーザー分の登録を backup_dir\\日時\\ に残す。中身: startup.reg（書き戻し用）、Startup\\（フォルダのコピー）。

    古いものは keep 個を残してゴミ箱へ。作ったフォルダのパスを返す。
    """
    dst = os.path.join(backup_dir, time.strftime("startup-%Y%m%d-%H%M%S"))
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(backup_dir, time.strftime("startup-%Y%m%d-%H%M%S") + f"-{n}")
    os.makedirs(dst)
    hkcu = "HKEY_CURRENT_USER\\"
    sections = [(hkcu + RUN_KEY, _read_values(winreg.HKEY_CURRENT_USER, RUN_KEY))]
    for sub in ("Run", "StartupFolder"):
        path = APPROVED_KEY + "\\" + sub
        sections.append((hkcu + path, _read_values(winreg.HKEY_CURRENT_USER, path)))
    with open(os.path.join(dst, "startup.reg"), "w", encoding="utf-16") as f:   # regedit が読める UTF-16（BOM 付き）
        f.write(reg_text(sections))
    if os.path.isdir(USER_FOLDER):
        shutil.copytree(USER_FOLDER, os.path.join(dst, "Startup"))
    # 自分で作ったバックアップ（startup-*）だけを対象にする。ほかのファイルには触らない
    mine = sorted(d for d in os.listdir(backup_dir) if d.startswith("startup-") and os.path.isdir(os.path.join(backup_dir, d)))
    for old in mine[:-keep] if keep > 0 else []:
        config.move_to_trash(os.path.join(backup_dir, old))
    return dst
