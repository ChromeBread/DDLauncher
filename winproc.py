"""Windows 依存処理: 管理者判定 / プロセス起動 / 起動状態 / スタートアップ登録。"""
import ctypes
import json
import os
import socket
import sys
import winreg
from ctypes import wintypes

import psutil

from favicon import is_url
from i18n import tr

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

_shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
_shell32.ShellExecuteW.restype = ctypes.c_ssize_t
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

SW_SHOWNORMAL = 1
SW_SHOWMINNOACTIVE = 7     # 最小化・フォーカスを奪わない
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DDLauncher"


def animations_enabled() -> bool:
    """Windows の「アニメーション効果」が ON か（OFF の人には色を動かさない）。読めなければ ON 扱い。"""
    on = wintypes.BOOL(True)
    if not ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0):   # SPI_GETCLIENTAREAANIMATION
        return True
    return bool(on.value)


def is_admin() -> bool:
    """このランチャー自身が管理者権限か。"""
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch(app: dict, admin: bool | None = None) -> tuple[bool, str]:
    """ShellExecute で起動。admin=None ならアプリ設定に従う。

    ランチャー自体は通常権限で動かす前提。"open" は通常権限、"runas" は UAC 経由で昇格。
    """
    path = app.get("path", "").strip()
    if not path:
        return False, tr("launch.no_path")
    use_admin = app.get("admin") if admin is None else admin
    workdir = app.get("workdir") or (os.path.dirname(path) if os.path.isfile(path) else None)
    show = SW_SHOWMINNOACTIVE if app.get("minimized") else SW_SHOWNORMAL
    args = app.get("args") or None
    browser = app.get("browser", "")
    if browser and is_url(path):   # Web アドレスを指定のブラウザで開く: ブラウザの exe に URL を渡す
        path, args, workdir = browser, f'"{path}"' + (" " + args if args else ""), None
    rc = _shell32.ShellExecuteW(None, "runas" if use_admin else "open", path, args, workdir, show)
    if rc <= 32:  # 5=拒否（UAC キャンセル含む）, 2/3=ファイルなし
        return False, tr("launch.failed", rc=rc)
    return True, ""


def browsers() -> list[tuple[str, str]]:
    """Windows に登録されているブラウザ: [(表示名, exe のパス)]。「既定のアプリ」の一覧と同じ場所を読む。"""
    found: dict[str, str] = {}
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Clients\StartMenuInternet") as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    key = winreg.EnumKey(k, i)
                    try:
                        name = winreg.QueryValue(k, key) or key
                        cmd = winreg.QueryValue(k, key + r"\shell\open\command").strip()
                    except OSError:
                        continue
                    exe = cmd[1:].split('"', 1)[0] if cmd.startswith('"') else cmd.split(" ", 1)[0]
                    if os.path.isfile(exe) and os.path.basename(exe).lower() != "iexplore.exe":   # IE は今は Edge に飛ぶだけ
                        found.setdefault(name, exe)   # ユーザー単位の登録を優先
        except OSError:
            pass
    return sorted(found.items(), key=lambda kv: kv[0].lower())


OBS_EXES = ("obs64.exe", "obs32.exe", "obs.exe")


def obs_guarded(app: dict, snap: dict) -> bool:
    """OBS 起動中のクリックを止めるアプリか（OBS 自身は止めない）。snap は snapshot() の結果。"""
    return bool(app.get("obs_guard")) and process_name(app) not in OBS_EXES and any(e in snap for e in OBS_EXES)


def process_name(app: dict) -> str:
    return (app.get("process_name") or os.path.basename(app.get("path", ""))).lower()


def snapshot() -> dict[str, list[int]]:
    """{exe名(小文字): [pid,...]}"""
    procs: dict[str, list[int]] = {}
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if name:
            procs.setdefault(name, []).append(p.pid)
    return procs


def is_elevated(pid: int) -> bool | None:
    """他プロセスが昇格しているか。None=判定不能。

    shortcut: 通常権限からは昇格プロセスのトークンを開けない（ACCESS_DENIED）ので、それを「昇格」とみなす
    ヒューリスティック。別ユーザーのプロセスも昇格扱いになるが、配信 PC では実害なし。
    """
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return True
        try:
            elev, size = wintypes.DWORD(), wintypes.DWORD()
            ok = _adv.GetTokenInformation(tok, TOKEN_ELEVATION, ctypes.byref(elev), 4, ctypes.byref(size))
            return bool(elev.value) if ok else None
        finally:
            _k32.CloseHandle(tok)
    finally:
        _k32.CloseHandle(h)


def app_state(app: dict, snap: dict[str, list[int]]) -> tuple[bool, bool | None]:
    """(起動中か, 昇格しているか)"""
    pids = snap.get(process_name(app), []) if app.get("path") else []
    if not pids:
        return False, None
    return True, is_elevated(pids[0])


# ---- モニタ / ウィンドウ移動 ----

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
_user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]

GW_OWNER = 4
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1


def _monitor_info(hmon) -> dict:
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    _user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    r, w = info.rcMonitor, info.rcWork
    return {"name": info.szDevice, "rect": (r.left, r.top, r.right, r.bottom),
            "work": (w.left, w.top, w.right, w.bottom), "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY)}


def monitors(labels: bool = False) -> list[dict]:
    """[{name: '\\\\.\\DISPLAY1', label, rect, work, primary}, ...]（物理ピクセル）。

    labels=True のときだけ、label に製品名（例: PL2374HD）を入れる（取れなければ ""）。ウィンドウを動かすときは要らないので省く。
    """
    out = []

    def cb(hmon, _hdc, _rect, _lp):
        out.append(_monitor_info(hmon))
        return True

    _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(cb), 0)
    names = monitor_labels() if labels else {}
    for m in out:
        m["label"] = names.get(m["name"], "")
    return out


# ---- モニタの製品名（DisplayConfig API。モニタの EDID に入っている名前） ----

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class _PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE), ("targetInfo", _PATH_TARGET), ("flags", ctypes.c_uint32)]


class _DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID), ("id", ctypes.c_uint32)]


class _SOURCE_NAME(ctypes.Structure):
    _fields_ = [("header", _DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class _TARGET_NAME(ctypes.Structure):
    _fields_ = [("header", _DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


_MODE_INFO_SIZE = 64   # DISPLAYCONFIG_MODE_INFO（中身は使わないので大きさだけ合わせる）
QDC_ONLY_ACTIVE_PATHS = 2
_GET_SOURCE_NAME, _GET_TARGET_NAME = 1, 2


def monitor_labels() -> dict[str, str]:
    """{'\\\\.\\DISPLAY1': 'PL2374HD', ...}。取れなかったモニタは入らない。失敗しても空を返すだけ。"""
    try:
        n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
        if _user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)) != 0:
            return {}
        paths = (_PATH_INFO * max(n_paths.value, 1))()
        modes = ctypes.create_string_buffer(_MODE_INFO_SIZE * max(n_modes.value, 1))
        if _user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                      ctypes.byref(n_modes), modes, None) != 0:
            return {}
        out = {}
        for p in paths[:n_paths.value]:
            src = _SOURCE_NAME()
            src.header = _DEVICE_INFO_HEADER(_GET_SOURCE_NAME, ctypes.sizeof(src), p.sourceInfo.adapterId, p.sourceInfo.id)
            tgt = _TARGET_NAME()
            tgt.header = _DEVICE_INFO_HEADER(_GET_TARGET_NAME, ctypes.sizeof(tgt), p.targetInfo.adapterId, p.targetInfo.id)
            if (_user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) == 0
                    and _user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt)) == 0):
                label = tgt.monitorFriendlyDeviceName.strip()
                if label and src.viewGdiDeviceName not in out:   # 複製表示（同じ画面を 2 台）は最初の 1 台の名前
                    out[src.viewGdiDeviceName] = label
        return out
    except (OSError, AttributeError, ValueError):
        return {}


def find_windows(proc: str) -> list[int]:
    """プロセス名に一致する、表示中のトップレベルウィンドウ（タイトルあり・オーナーなし）。"""
    pids = set(snapshot().get(proc.lower(), []))
    found = []

    def cb(hwnd, _lp):
        if _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, GW_OWNER) \
                and _user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                found.append(hwnd)
        return True

    if pids:
        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def move_to_monitor(hwnd: int, device: str) -> bool:
    """ウィンドウを指定モニタへ移す（モニタ内の相対位置を保つ。最小化/最大化状態も維持）。

    管理者権限のウィンドウは UIPI により通常権限からは動かせず False になる。
    shortcut: rcNormalPosition はワークスペース座標なので、メインモニタのタスクバーが上/左にあると
    その幅だけずれる。問題になったら SetWindowPos による補正を追加する。
    """
    dst = next((m for m in monitors() if m["name"] == device), None)
    if dst is None:
        return False
    src = _monitor_info(_user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
    if src["name"] == device:
        return True
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not _user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    r = wp.rcNormalPosition
    dl, dt, dr, db = dst["work"]
    w, h = min(r.right - r.left, dr - dl), min(r.bottom - r.top, db - dt)
    x = max(dl, min(dl + (r.left - src["work"][0]), dr - w))
    y = max(dt, min(dt + (r.top - src["work"][1]), db - h))
    wp.rcNormalPosition = wintypes.RECT(x, y, x + w, y + h)
    wp.flags = 0
    return bool(_user32.SetWindowPlacement(hwnd, ctypes.byref(wp)))


# ---- ポート管理 ----

# 機器探しの共用 UDP（NetBIOS / SSDP / WS-Discovery / mDNS / LLMNR）。Windows やブラウザも同じ番号を同時に使うので、
# どのアプリの物でもない。拾うと「Shoost が 5353 を使用中 → Edge と衝突」のような誤った衝突になる
SHARED_UDP_PORTS = {137, 138, 1900, 3702, 5353, 5355}


def listening_ports() -> dict[int, list[tuple[int, str, str]]]:
    """{port: [(pid, プロセス名, 'TCP'|'UDP'), ...]}  TCP は LISTEN のみ、共用の UDP は除く。管理者不要。"""
    names: dict[int, str] = {}
    out: dict[int, list[tuple[int, str, str]]] = {}
    for c in psutil.net_connections("inet"):
        if not c.laddr:
            continue
        tcp = c.type == socket.SOCK_STREAM
        if tcp and c.status != psutil.CONN_LISTEN:
            continue
        if not tcp and c.laddr.port in SHARED_UDP_PORTS:
            continue
        pid = c.pid or 0
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name()
            except psutil.Error:
                names[pid] = f"pid{pid}"
        entry = (pid, names[pid], "TCP" if tcp else "UDP")
        lst = out.setdefault(c.laddr.port, [])
        if entry not in lst:
            lst.append(entry)
    # HTTP.sys で待ち受けるソフト（棒読みちゃんの HTTP、ゆかコネ NEO など）は持ち主が System(4) に見えるので、本当の持ち主に置き換える
    if any(pid == 4 and proto == "TCP" for owners in out.values() for pid, _, proto in owners):
        for port, pids in http_sys_owners().items():
            if port in out:
                real = []
                for pid in pids:
                    try:
                        real.append((pid, psutil.Process(pid).name(), "TCP"))
                    except psutil.Error:
                        pass
                if real:
                    out[port] = [o for o in out[port] if not (o[0] == 4 and o[2] == "TCP")] + real
    return out


def parse_http_sys(text: str) -> dict[int, list[int]]:
    """`netsh http show servicestate view=requestq` の出力 → {port: [pid, ...]}。見出しの訳に頼らず数字と URL だけ読む。"""
    import re
    out: dict[int, list[int]] = {}
    pid = None
    for line in text.splitlines():
        m = re.match(r"\s*\S+[:：]\s*(\d+)\D.*\.exe\s*$", line, re.I)   # "ID: 35072, image: ...exe" / "ID: 35072、イメージ: ...exe"
        if m:
            pid = int(m.group(1))
            continue
        m = re.search(r"https?://(?:\[[^\]]*\]|[^:/\s]+):(\d+)", line, re.I)
        if m and pid:
            lst = out.setdefault(int(m.group(1)), [])
            if pid not in lst:
                lst.append(pid)
    return out


def http_sys_owners() -> dict[int, list[int]]:
    import subprocess
    try:
        r = subprocess.run(["netsh", "http", "show", "servicestate", "view=requestq"], capture_output=True,
                           timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        text = r.stdout.decode("utf-8")   # Windows の「UTF-8 を使用」設定だと UTF-8、そうでなければ ANSI で出てくる
    except UnicodeDecodeError:
        text = r.stdout.decode("mbcs", errors="replace")
    return parse_http_sys(text)


OBS_WS_CONFIG = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "plugin_config", "obs-websocket", "config.json")


def obs_websocket_port(path: str = OBS_WS_CONFIG) -> int | None:
    """OBS の WebSocket サーバーのポート（OBS が止まっていても設定ファイルから読める）。"""
    try:
        with open(path, encoding="utf-8") as f:
            port = int(json.load(f)["server_port"])
        return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---- IP アドレスの割り当て方式（固定 / 自動）。IP アドレスそのものは読まない ----

_VIRTUAL_WORDS = ("virtual", "hyper-v", "vpn", "tap-", "vmware", "virtualbox", "wireguard", "tailscale",
                  "zerotier", "hamachi", "loopback", "bluetooth")


def classify_ip_mode(adapters: list[dict]) -> str | None:
    """adapters: [{type, up, dhcp, gateway, desc}]。インターネットへ出る実ネットワークだけで判定する。"""
    modes = {"dynamic" if a["dhcp"] else "static" for a in adapters
             if a["up"] and a["gateway"] and a["type"] in (6, 71)   # 6 = 有線, 71 = Wi-Fi
             and not any(w in a["desc"].lower() for w in _VIRTUAL_WORDS)}
    if not modes:
        return None
    return modes.pop() if len(modes) == 1 else "mixed"


def _adapters() -> list[dict]:
    """GetAdaptersAddresses で種類・接続状態・DHCP の有無・既定ゲートウェイの有無だけを読む。"""
    from ctypes import wintypes

    class ADDR(ctypes.Structure):
        pass
    ADDR._fields_ = [("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(ADDR)),
                     ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.c_void_p),
                     ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
                     ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", wintypes.LPWSTR),
                     ("Description", wintypes.LPWSTR), ("FriendlyName", wintypes.LPWSTR),
                     ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.DWORD),
                     ("Flags", wintypes.DWORD), ("Mtu", wintypes.DWORD), ("IfType", wintypes.DWORD),
                     ("OperStatus", ctypes.c_int), ("Ipv6IfIndex", wintypes.DWORD),
                     ("ZoneIndices", wintypes.DWORD * 16), ("FirstPrefix", ctypes.c_void_p),
                     ("TransmitLinkSpeed", ctypes.c_ulonglong), ("ReceiveLinkSpeed", ctypes.c_ulonglong),
                     ("FirstWinsServerAddress", ctypes.c_void_p), ("FirstGatewayAddress", ctypes.c_void_p)]
    AF_INET, GAA_FLAG_INCLUDE_GATEWAYS, DHCP_ENABLED = 2, 0x80, 0x4
    get = ctypes.windll.iphlpapi.GetAdaptersAddresses
    size = wintypes.ULONG(16 * 1024)
    for _ in range(3):   # バッファが足りなければ指定サイズで取り直す
        buf = ctypes.create_string_buffer(size.value)
        ret = get(AF_INET, GAA_FLAG_INCLUDE_GATEWAYS, None, buf, ctypes.byref(size))
        if ret == 0:
            break
        if ret != 111:   # ERROR_BUFFER_OVERFLOW 以外は諦める
            return []
    else:
        return []
    out, p = [], ctypes.cast(buf, ctypes.POINTER(ADDR))
    while p:
        a = p.contents
        out.append({"type": a.IfType, "up": a.OperStatus == 1, "dhcp": bool(a.Flags & DHCP_ENABLED),
                    "gateway": bool(a.FirstGatewayAddress), "desc": a.Description or ""})
        p = a.Next
    return out


def detect_ip_mode() -> str | None:
    """static / dynamic / mixed。判定できなければ None。"""
    try:
        return classify_ip_mode(_adapters())
    except (OSError, AttributeError, ValueError):
        return None


def detect_app_ports(apps: list[dict], listening: dict) -> list[dict]:
    """登録アプリが使うポートを集める: OBS は設定ファイルから、ほかは起動中なら待ち受けているポートから。"""
    found: dict[int, dict] = {}
    for app in apps:
        if not app.get("path"):
            continue
        exe = process_name(app)
        name = app.get("name") or exe
        if exe in ("obs64.exe", "obs32.exe", "obs.exe"):
            port = obs_websocket_port()
            if port:
                found.setdefault(port, {"port": str(port), "memo": f"{name} (WebSocket)", "process": exe, "src": "config"})
        for port, owners in listening.items():
            if any(pname.lower() == exe for _, pname, _ in owners):
                found.setdefault(port, {"port": str(port), "memo": name, "process": exe, "src": "running"})
    return [found[p] for p in sorted(found)]


# ---- 配信でよく使うソフトのポート（設定ファイルから読む。起動していなくても分かるものは分かる） ----
# 読むだけで書き換えない。読めなければ黙って飛ばす（相手のソフトの版が変わって形式が違っても落ちない）

ONECOMME_CONFIG = os.path.join(os.environ.get("APPDATA", ""), "onecomme", "config.json")
ONECOMME_PORT = 11180   # わんコメの API / テンプレート配信用サーバー（固定）
YNC_CONFIG = os.path.join(os.environ.get("APPDATA", ""), "YukarinetteConnector", "YNC_Neo.config")
BOUYOMI_EXE = "bouyomichan.exe"
ONECOMME_EXE = "onecomme.exe"
YNC_EXE = "ync_neo.exe"


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _valid_port(v) -> int | None:
    try:
        port = int(float(v))   # ソフトによっては 50001.0 のように小数で保存している
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def bouyomi_setting_ports(setting_path: str) -> list[tuple[int, str]]:
    """BouyomiChan.setting（XML）から有効になっているサーバーのポート: [(port, 'Socket'|'HTTP')]。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(setting_path).getroot()
    except (OSError, ET.ParseError):
        return []
    out = []
    for enable, key, label in (("EnableSocket", "PortNumber", "Socket"), ("EnableHttpd", "PortNumberHttp", "HTTP")):
        port = _valid_port(root.findtext(key))
        if port and (root.findtext(enable) or "").strip().lower() == "true":
            out.append((port, label))
    return out


def onecomme_bouyomi(cfg: dict | None) -> tuple[str, int | None]:
    """わんコメの棒読みちゃん連携設定: (棒読みちゃんの exe パス, 送り先ポート)。連携がオフならポートは None。"""
    try:
        b = cfg["config"]["integration"]["bouyomichan"]
    except (KeyError, TypeError):
        return "", None
    if not isinstance(b, dict):
        return "", None
    return str(b.get("exePath") or ""), _valid_port(b.get("port")) if b.get("enabled") else None


def ync_dynamic_port(cfg: dict | None) -> bool:
    """ゆかりネットコネクター NEO が起動のたびにポートを選び直す設定か。"""
    return bool(cfg and cfg.get("dynamicPortMode"))


def _running_exe_paths(exe: str) -> list[str]:
    out = []
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == exe:
            try:
                out.append(p.exe())
            except psutil.Error:
                pass
    return out


def scan_known_ports(apps: list[dict], listening: dict) -> tuple[list[dict], list[str]]:
    """登録アプリ + OBS / わんコメ / 棒読みちゃん / ゆかりネットコネクター NEO のポートを集める。

    返り値: ([{port, memo, process, src}], [補足メッセージ])。src: config（設定ファイル）/ default（既定値）/ running（起動中）
    同じ番号が重なったら先に見つけた方（設定ファイル > 起動中）を残す。
    """
    found = {int(p["port"]): p for p in detect_app_ports(apps, listening)}
    notes = []

    def add(port, memo, exe, src):
        found.setdefault(port, {"port": str(port), "memo": memo, "process": exe, "src": src})

    def add_running(exe, memo):
        for port in ports_of(exe, listening):
            add(port, memo, exe, "running")

    # OBS（登録していなくても、WebSocket の設定ファイルがあれば読む）
    port = obs_websocket_port()
    if port:
        add(port, "OBS (WebSocket)", "obs64.exe", "config")

    # わんコメ: 自分のサーバーは固定。棒読みちゃんの場所と送り先ポートも設定に入っている
    oc = _read_json(ONECOMME_CONFIG)
    oc_bouyomi_exe, oc_bouyomi_port = onecomme_bouyomi(oc)
    if oc is not None:
        add(ONECOMME_PORT, tr("ports.src.onecomme"), "OneComme.exe", "default")
    add_running(ONECOMME_EXE, tr("ports.src.onecomme"))

    # 棒読みちゃん: exe と同じフォルダの BouyomiChan.setting。exe の場所は登録アプリ → 起動中 → わんコメの連携設定の順で探す
    candidates = [a["path"] for a in apps if process_name(a) == BOUYOMI_EXE and a.get("path")]
    candidates += _running_exe_paths(BOUYOMI_EXE) + ([oc_bouyomi_exe] if oc_bouyomi_exe else [])
    bouyomi = []
    for exe_path in dict.fromkeys(os.path.normcase(os.path.abspath(c)) for c in candidates):
        bouyomi = bouyomi_setting_ports(os.path.join(os.path.dirname(exe_path), "BouyomiChan.setting"))
        if bouyomi:
            break
    for port, label in bouyomi:
        add(port, f"{tr('ports.src.bouyomi')} ({label})", "BouyomiChan.exe", "config")
    add_running(BOUYOMI_EXE, tr("ports.src.bouyomi"))
    http = [p for p, label in bouyomi if label == "HTTP"]
    if oc_bouyomi_port and http and oc_bouyomi_port not in http:
        notes.append(tr("ports.note.bouyomi_mismatch", onecomme=oc_bouyomi_port, bouyomi=http[0]))

    # ゆかりネットコネクター NEO: ポートは設定に残らないので、起動中に待ち受けているポートを拾う
    # 「使える通信ポートを自動で選びます」がオンなら、番号は起動のたびに変わる
    ync = _read_json(YNC_CONFIG)
    auto = ync_dynamic_port(ync)
    add_running(YNC_EXE, tr("ports.src.ync") + (tr("ports.ync_auto") if auto else ""))
    running = bool(ports_of(YNC_EXE, listening))
    if ync is not None and not running:
        notes.append(tr("ports.note.ync_dynamic" if auto else "ports.note.ync_stopped", detect=tr("ports.detect")))
    elif auto and running:
        notes.append(tr("ports.note.ync_auto_running"))
    return [found[p] for p in sorted(found)], notes


def ports_of(exe: str, listening: dict) -> list[int]:
    """そのプロセス名が今待ち受けているポート（小さい順）。"""
    exe = exe.strip().lower()
    return sorted(p for p, owners in listening.items() if exe and any(n.lower() == exe for _, n, _ in owners))


def port_status(expected: str, owners: list[tuple[int, str, str]], elsewhere: list[int] = ()) -> tuple[str, str]:
    """(level, 表示文)。level: free / used / conflict（想定外のプロセスが使用中）/ moved（想定プロセスが別の番号で動作中）

    elsewhere: 想定プロセスが今待ち受けているポート。ポートを自動で選ぶソフトは起動のたびに番号が変わるため。
    """
    if not owners:
        if elsewhere:
            return "moved", tr("port.moved", ports=", ".join(map(str, elsewhere)))
        return "free", tr("port.free")
    desc = ", ".join(f"{name}({pid}/{proto})" for pid, name, proto in owners)
    exp = expected.strip().lower()
    if exp and any(name.lower() != exp for _, name, _ in owners):
        return "conflict", tr("port.conflict", desc=desc)
    return "used", tr("port.used", desc=desc)


def release_port(pid: int) -> tuple[bool, str]:
    """ポートを掴んでいるプロセスを終了して解放する。"""
    if pid <= 4 or pid == os.getpid():   # 0=Idle, 4=System
        return False, tr("release.system")
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        p.wait(3)
        return True, tr("release.done", name=name)
    except psutil.NoSuchProcess:
        return True, tr("release.gone")
    except psutil.AccessDenied:
        return False, tr("release.denied")
    except psutil.TimeoutExpired:
        return False, tr("release.timeout")


# ---- Windows スタートアップ（HKCU\...\Run。管理者不要） ----

def startup_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}" --autostart'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    main = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{pyw}" "{main}" --autostart'


def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
