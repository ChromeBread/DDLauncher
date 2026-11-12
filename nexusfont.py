"""NexusFont のセット・ライブラリとフォントファイルを zip 1 つにまとめ、別の PC で読み込む（Debug Mode の機能）。

zip の中身:
  nexusfont/library.xml ほか   NexusFont の userdata（nexusfont.ini はウィンドウ位置を除く）
  Sets/<セット名>/<元のフォルダ構造>/<フォント>   例: Sets/作業1/Fonts/フリー/A.ttf（ドライブ名は外す）
  Folders/<元のフォルダ構造>/...                 ライブラリのフォルダのうち選んだもの（中身を丸ごと）
  ddlauncher-nexusfont.json                      元のパス → zip 内の場所（読み込み時に library.xml を書き換えるため）

元のフォントや NexusFont のファイルは、書き出しでは読むだけ。読み込みで置き換えるものは先にバックアップを取る。
"""
import json
import os
import re
import shutil
import time
import zipfile
import zlib
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, unescape

DATA_FILES = ["library.xml", "tags.list", "samples.history", "nexusfont.ini"]
DATA_DIR, SETS_DIR, FOLDERS_DIR = "nexusfont", "Sets", "Folders"
MANIFEST = "ddlauncher-nexusfont.json"
CHUNK = 1024 * 1024


def lp(path: str) -> str:
    """260 文字を超えるパスでも開けるよう \\\\?\\ を付ける（日本語の長いフォルダ名が多いため）。"""
    path = os.path.abspath(path)
    return "\\\\?\\" + path if len(path) >= 240 and not path.startswith("\\\\") else path


def find_userdata() -> str | None:
    """インストール済み NexusFont の userdata フォルダ。見つからなければ None（ポータブル版は画面で選んでもらう）。"""
    import winreg
    keys = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as app:
                            if not str(winreg.QueryValueEx(app, "DisplayName")[0]).lower().startswith("nexusfont"):
                                continue
                            folder = winreg.QueryValueEx(app, "InstallLocation")[0]
                    except OSError:
                        continue
                    if folder and os.path.isfile(os.path.join(folder, "nexusfont.exe")):
                        return os.path.join(folder, "userdata")
        except OSError:
            pass
    return None


def userdata_of(folder: str) -> str:
    """利用者が選んだフォルダ（nexusfont.exe のある場所 / userdata そのもの）→ userdata。"""
    return folder if os.path.basename(folder.rstrip("\\/")).lower() == "userdata" else os.path.join(folder, "userdata")


def read_library(userdata: str) -> dict:
    """{"sets": {セット名: [フォントのパス]}, "folders": [ライブラリのフォルダ]}。重複は除き、並びは保つ。"""
    root = ET.parse(os.path.join(userdata, "library.xml")).getroot()
    sets = {}
    for s in root.iter("set"):
        sets[s.get("name", "")] = list(dict.fromkeys((f.text or "").strip() for f in s.iter("font") if (f.text or "").strip()))
    folders = list(dict.fromkeys((f.text or "").strip() for f in root.iter("folder") if (f.text or "").strip()))
    return {"sets": sets, "folders": folders}


def rel_path(path: str) -> str:
    """D:\\Fonts\\A.ttf → Fonts/A.ttf（zip 内では元のフォルダ構造をドライブ名抜きで使う）。"""
    return os.path.splitdrive(path)[1].replace("\\", "/").strip("/")


def safe_name(name: str) -> str:
    """セット名をフォルダ名に使えるようにする。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .") or "_"


def scan_folder(folder: str) -> list[tuple[str, int]]:
    """フォルダの中のファイル（下の階層も）と大きさ。"""
    out = []
    for dirpath, _, files in os.walk(lp(folder)):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                out.append((p.removeprefix("\\\\?\\"), os.path.getsize(p)))
            except OSError:
                pass
    return out


def plan_export(lib: dict, sets: list[str], folder_files: dict[str, list[tuple[str, int]]]) -> dict:
    """書き出す (元のパス, zip 内の場所, 大きさ) と、見つからないフォント、manifest を作る。

    folder_files = {選んだフォルダ: scan_folder の結果}
    """
    entries, missing, used = [], [], set()
    manifest = {"version": 1, "sets": {}, "folders": {}}

    def arc_for(top: str, path: str) -> str:
        arc = f"{top}/{rel_path(path)}"
        if arc.lower() in used:   # 別ドライブに同じ構造があったときだけドライブ名を足す
            arc = f"{top}/{os.path.splitdrive(path)[0].strip(':') or '_'}/{rel_path(path)}"
        used.add(arc.lower())
        return arc

    for name in sets:
        top = f"{SETS_DIR}/{safe_name(name)}"
        m = manifest["sets"].setdefault(name, {})
        for path in lib["sets"].get(name, []):
            try:
                size = os.path.getsize(lp(path))
            except OSError:
                missing.append(path)
                continue
            m[path] = arc_for(top, path)
            entries.append((path, m[path], size))
    for folder, files in folder_files.items():
        base = arc_for(FOLDERS_DIR, folder)
        manifest["folders"][folder] = base
        for path, size in files:
            entries.append((path, f"{base}/{os.path.relpath(path, folder).replace(os.sep, '/')}", size))
    return {"entries": entries, "missing": missing, "manifest": manifest, "total": sum(e[2] for e in entries)}


def measure_speed(entries: list, dest_dir: str, seconds: float = 2.0, limit: int = 256 * CHUNK) -> float:
    """書き出しの速さ（バイト/秒）の目安。選んだフォントを最大 seconds 秒 / limit バイト読み、保存先に試しに書いて測る。

    試しに書いたファイルは自分で作った一時ファイルなので、測り終えたらすぐ消す。
    """
    tmp = os.path.join(dest_dir, f".ddlauncher-speedtest-{os.getpid()}.tmp")
    done, t0 = 0, time.monotonic()
    try:
        with open(lp(tmp), "wb") as out:
            for src, _arc, _size in entries:
                with open(lp(src), "rb") as f:
                    while (chunk := f.read(CHUNK)) and done < limit and time.monotonic() - t0 < seconds:
                        out.write(chunk)
                        done += len(chunk)
                if done >= limit or time.monotonic() - t0 >= seconds:
                    break
            out.flush()
            os.fsync(out.fileno())   # Windows のキャッシュに溜めただけの速さにならないように
        return done / max(time.monotonic() - t0, 0.001)
    finally:
        try:
            os.remove(lp(tmp))
        except OSError:
            pass


def strip_section(ini: str, section: str) -> str:
    """ini から [section] を丸ごと外す（ウィンドウ位置はモニタ構成が違う PC だと画面外に開くため）。"""
    out, skip = [], False
    for line in ini.splitlines(keepends=True):
        if line.strip().startswith("["):
            skip = line.strip().lower() == f"[{section.lower()}]"
        if not skip:
            out.append(line)
    return "".join(out)


def get_section(ini: str, section: str) -> str:
    out, keep = [], False
    for line in ini.splitlines(keepends=True):
        if line.strip().startswith("["):
            keep = line.strip().lower() == f"[{section.lower()}]"
        if keep:
            out.append(line)
    return "".join(out)


def write_export(zip_path: str, userdata: str, plan: dict, progress=None, cancelled=lambda: False, asis: bool = False) -> bool:
    """zip を書き出す。中止したら False（書きかけの一時ファイルは消す。元のファイルには触れていない）。

    フォントはほとんど縮まないので無圧縮（STORED）。必要な容量が事前に正確に分かる。
    asis = 「そのまま」: 読み込み時に元と同じパスへ戻し、library.xml も ini（ウィンドウ位置）も書き換えない。
    """
    tmp = zip_path + ".tmp"
    done = 0
    try:
        with zipfile.ZipFile(lp(tmp), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for name in DATA_FILES:
                p = os.path.join(userdata, name)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        data = f.read()
                    if name == "nexusfont.ini" and not asis:
                        data = strip_section(data.decode("utf-8-sig"), "Window").encode("utf-8-sig")
                    zf.writestr(f"{DATA_DIR}/{name}", data)
            zf.writestr(MANIFEST, json.dumps(dict(plan["manifest"], asis=asis), ensure_ascii=False, indent=2))
            for src, arc, _size in plan["entries"]:
                if cancelled():
                    raise InterruptedError
                info = zipfile.ZipInfo.from_file(lp(src), arc)
                info.compress_type = zipfile.ZIP_STORED
                with open(lp(src), "rb") as f, zf.open(info, "w", force_zip64=True) as out:
                    while chunk := f.read(CHUNK):
                        out.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, arc)
                        if cancelled():
                            raise InterruptedError
        os.replace(lp(tmp), lp(zip_path))
        return True
    except InterruptedError:
        try:
            os.remove(lp(tmp))   # 自分が作った書きかけの zip だけ（数十 GB をゴミ箱に送らない）
        except OSError:
            pass
        return False


def read_manifest(zf: zipfile.ZipFile) -> dict | None:
    try:
        m = json.loads(zf.read(MANIFEST).decode("utf-8"))
        return m if isinstance(m, dict) and m.get("version") == 1 else None
    except (KeyError, ValueError):
        return None


def target_of(arc: str, dest: str) -> str:
    return os.path.join(dest, *arc.split("/"))


def targets(zf: zipfile.ZipFile, manifest: dict, dest: str | None) -> list:
    """zip のフォント → 置く場所の [(info, パス)]。dest=None は「そのまま」（manifest の元のパスへ戻す）。"""
    back = {arc: orig for m in manifest.get("sets", {}).values() for orig, arc in m.items()}
    folders = [(arc + "/", orig) for orig, arc in manifest.get("folders", {}).items()]
    out, seen = [], set()
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or not name.startswith((SETS_DIR + "/", FOLDERS_DIR + "/")):
            continue
        if ".." in name.split("/") or ":" in name or "\\" in name:
            continue   # zip の外へ書かせない
        if dest is not None:
            t = target_of(name, dest)
        else:
            t = back.get(name) or next((os.path.join(o, *name[len(a):].split("/")) for a, o in folders if name.startswith(a)), None)
            if not t or not os.path.isabs(t) or ".." in t.replace("/", "\\").split("\\"):
                continue
        if t.lower() not in seen:   # 「そのまま」ではセットとフォルダで同じファイルを指すことがある
            seen.add(t.lower())
            out.append((info, t))
    return out


def missing_drives(manifest: dict) -> list[str]:
    """「そのまま」で戻すとき、この PC に無いドライブ（D: など）。"""
    paths = [p for m in manifest.get("sets", {}).values() for p in m] + list(manifest.get("folders", {}))
    drives = sorted({os.path.splitdrive(p)[0].upper() for p in paths if os.path.splitdrive(p)[0]})
    return [d for d in drives if not os.path.exists(d + "\\")]


def _crc(path: str) -> int:
    c = 0
    with open(lp(path), "rb") as f:
        while chunk := f.read(CHUNK):
            c = zlib.crc32(chunk, c)
    return c


def plan_import(zf: zipfile.ZipFile, manifest: dict, dest: str | None, cancelled=lambda: False) -> dict:
    """置く場所ごとに、新規 / 同じ（飛ばす）/ 中身が違う に分ける。各要素は (info, パス)。"""
    plan = {"new": [], "same": [], "differ": []}
    for info, t in targets(zf, manifest, dest):
        if cancelled():
            raise InterruptedError
        if not os.path.exists(lp(t)):
            plan["new"].append((info, t))
        elif os.path.getsize(lp(t)) == info.file_size and _crc(t) == info.CRC:
            plan["same"].append((info, t))
        else:
            plan["differ"].append((info, t))
    return plan


def extract(zf: zipfile.ZipFile, items: list, trash, progress=None, cancelled=lambda: False) -> bool:
    """items（plan_import の (info, パス)）を書く。既にあるファイルは trash(path) でゴミ箱へ移してから置く。中止したら False。"""
    done = 0
    for info, t in items:
        if cancelled():
            return False
        os.makedirs(lp(os.path.dirname(t)), exist_ok=True)
        tmp = t + ".ddl-tmp"
        with zf.open(info) as src, open(lp(tmp), "wb") as out:
            while chunk := src.read(CHUNK):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, info.filename)
        if os.path.exists(lp(t)) and not trash(lp(t)):
            os.remove(lp(tmp))
            raise OSError(f"could not move to Recycle Bin: {t}")
        os.replace(lp(tmp), lp(t))
        mt = time.mktime(info.date_time + (0, 0, -1))
        os.utime(lp(t), (mt, mt))
    return True


def rewrite_library(xml: str, manifest: dict, dest: str) -> str:
    """library.xml の中のパスだけを、読み込み先のパスに書き換える（書式はそのまま残す）。

    zip に入れていないフォルダ / フォントは元のパスのまま（手でコピーした場合に備える）。
    """
    folders = {k.lower(): v for k, v in manifest.get("folders", {}).items()}
    sets = {name: {k.lower(): v for k, v in m.items()} for name, m in manifest.get("sets", {}).items()}
    current = [None]

    def sub(m: re.Match) -> str:
        if m.group("set") is not None:
            current[0] = unescape(m.group("set"), {"&quot;": '"', "&apos;": "'"})
            return m.group(0)
        tag, path = m.group("tag"), unescape(m.group("path")).strip()
        arc = (sets.get(current[0], {}) if tag == "font" else folders).get(path.lower())
        return f"<{tag}>{escape(target_of(arc, dest))}</{tag}>" if arc else m.group(0)

    return re.sub(r'<set\s+name="(?P<set>[^"]*)"|<(?P<tag>folder|font)>(?P<path>[^<]*)</(?P=tag)>', sub, xml)


def restore_data(zf: zipfile.ZipFile, userdata: str, manifest: dict, dest: str | None, backup_dir: str) -> str:
    """NexusFont の userdata を zip の内容で置き換える。今あるファイルは backup_dir/nexusfont-日時 にコピーしておく。

    dest=None（そのまま）なら library.xml も ini も zip の中身のまま置く。
    """
    os.makedirs(userdata, exist_ok=True)
    bdir = os.path.join(backup_dir, time.strftime("nexusfont-%Y%m%d-%H%M%S"))
    for name in DATA_FILES:
        p = os.path.join(userdata, name)
        if os.path.isfile(p):
            os.makedirs(bdir, exist_ok=True)
            shutil.copy2(p, os.path.join(bdir, name))
    for name in DATA_FILES:
        try:
            data = zf.read(f"{DATA_DIR}/{name}")
        except KeyError:
            continue
        p = os.path.join(userdata, name)
        if dest is None:
            pass
        elif name == "library.xml":
            data = rewrite_library(data.decode("utf-8"), manifest, dest).encode("utf-8")
        elif name == "nexusfont.ini" and os.path.isfile(p):   # この PC のウィンドウ位置は残す
            with open(p, encoding="utf-8-sig") as f:
                window = get_section(f.read(), "Window")
            text = data.decode("utf-8-sig")
            data = (window + ("" if window.endswith("\n") or not window else "\r\n") + text).encode("utf-8-sig")
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
    return bdir if os.path.isdir(bdir) else ""


def window_fix(ini: str, works: list[tuple[int, int, int, int]]) -> str | None:
    """[Window] のタイトルバーがどのモニタの作業領域にも 100px 以上かかっていなければ、works[0]（主モニタ）の左上へ寄せた ini を返す。

    画面内なら None。works は (left, top, right, bottom) の物理ピクセル。タイトルバーが見えればドラッグで動かせるので、そこだけで判定する。
    """
    sec = get_section(ini, "Window")
    vals = {k: int(v) for k, v in re.findall(r"(?mi)^(Left|Top|Width|Height)=(-?\d+)\s*$", sec)}
    if not sec or len(vals) < 4:
        return None
    l, t, w = vals["Left"], vals["Top"], vals["Width"]
    if any(min(l + w, r) - max(l, wl) >= 100 and wt <= t + 15 <= b for wl, wt, r, b in works):
        return None
    wl, wt, r, b = works[0]
    new = {"Left": wl + 50, "Top": wt + 50, "Width": min(w, r - wl - 100), "Height": min(vals["Height"], b - wt - 100)}
    fixed = re.sub(r"(?mi)^(Left|Top|Width|Height)=-?\d+", lambda m: f"{m.group(1)}={new[m.group(1).capitalize()]}", sec)
    return ini.replace(sec, fixed, 1)
