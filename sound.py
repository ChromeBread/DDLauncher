"""サウンドデバイス: 既定の出力 / 入力デバイス・ミュート・音量をプロファイルとして保存し、切り替える。

Windows の Core Audio（MMDevice）を ctypes で直接呼ぶ（依存ライブラリを増やさない）。
既定デバイスの変更は Windows 標準の「サウンド」設定と同じ IPolicyConfig を使う（非公開だが Vista 以降変わっていない）。

プロファイル:
  {"name": str,
   "defaults": {"render" | "capture": {"console" | "multimedia" | "communications": {"id", "name"}}},
   "levels": [{"id", "name", "flow", "mute": bool, "volume": 0.0〜1.0}]}
  id は Windows のデバイス ID（{0.0.0.00000000}.{GUID}）。別の PC では一致しないので、そのときは name（デバイス名）で探す。
"""
import ctypes
import os
from ctypes import POINTER, byref, c_float, c_void_p, wintypes

FLOWS = {"render": 0, "capture": 1}                          # 出力 / 入力
ROLES = {"console": 0, "multimedia": 1, "communications": 2}  # Windows の「既定のデバイス」は console + multimedia
SPR_KEYS = {("render", "console"): "DefaultRender", ("render", "multimedia"): "DefaultRenderMultimedia",
            ("render", "communications"): "DefaultRenderCommunications", ("capture", "console"): "DefaultCapture",
            ("capture", "multimedia"): "DefaultCaptureMultimedia", ("capture", "communications"): "DefaultCaptureCommunications"}


# ================= .spr / プロファイル（Windows に触らない部分） =================
def read_text(path: str) -> str:
    """.spr は Windows の ANSI（日本語なら Shift_JIS）で書かれている。UTF-8 / UTF-16 で保存されたものも読む。"""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("mbcs", errors="replace")


def parse_spr(text: str, name: str) -> dict:
    """SoundVolumeView のプロファイル（.spr）→ プロファイル。デバイス本体（Type=1）だけを読み、アプリごとの音量などは使わない。"""
    items, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = {}
            items.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    prof = {"name": name, "defaults": {"render": {}, "capture": {}}, "levels": []}
    for it in items:
        dev_id = it.get("ID", "")
        if it.get("Type") != "1" or not dev_id.startswith(("{0.0.0.", "{0.0.1.")):
            continue
        flow = "render" if dev_id.startswith("{0.0.0.") else "capture"
        dev = {"id": dev_id, "name": it.get("Name", "")}
        for (f, role), key in SPR_KEYS.items():
            if f == flow and it.get(key) == "1":
                prof["defaults"][flow][role] = dict(dev)
        try:
            volume = min(max(float(it.get("VolumeScalar", "")), 0.0), 1.0)
        except ValueError:
            volume = None
        prof["levels"].append(dict(dev, flow=flow, mute=it.get("Mute") == "1", volume=volume))
    return prof


def capture_profile(name: str, devices: list[dict], defaults: dict) -> dict:
    """今の状態 → プロファイル。devices = list_devices()、defaults = get_defaults()。"""
    by_id = {d["id"]: d for d in devices}
    prof = {"name": name, "defaults": {"render": {}, "capture": {}}, "levels": []}
    for flow, roles in defaults.items():
        for role, dev_id in roles.items():
            if dev_id in by_id:
                prof["defaults"][flow][role] = {"id": dev_id, "name": by_id[dev_id]["name"]}
    prof["levels"] = [{"id": d["id"], "name": d["name"], "flow": d["flow"], "mute": d["mute"], "volume": d["volume"]}
                      for d in devices]
    return prof


def resolve(entry: dict, devices: list[dict]) -> str | None:
    """プロファイルのデバイス → この PC のデバイス ID。ID が無ければ、同じ向きで名前が 1 つだけ一致するものを使う。"""
    flow = entry.get("flow") or ("capture" if entry.get("id", "").startswith("{0.0.1.") else "render")
    same_flow = [d for d in devices if d["flow"] == flow]
    if any(d["id"] == entry.get("id") for d in same_flow):
        return entry["id"]
    hits = [d["id"] for d in same_flow if entry.get("name") and entry["name"] in (d["name"], d["desc"])]
    return hits[0] if len(hits) == 1 else None   # 同じ名前が複数あるときは取り違えないよう諦める


def targets(prof: dict, devices: list[dict]) -> tuple[dict, list[str]]:
    """{(flow, role): この PC のデバイス ID} と、見つからなかったデバイス名。"""
    out, missing = {}, []
    for flow, roles in prof.get("defaults", {}).items():
        for role, entry in roles.items():
            dev_id = resolve(dict(entry, flow=flow), devices)
            if dev_id:
                out[(flow, role)] = dev_id
            elif entry.get("name") not in missing:
                missing.append(entry.get("name") or entry.get("id", ""))
    return out, missing


def apply_profile(prof: dict, devices: list[dict], set_default, set_level, with_levels: bool = True) -> tuple[dict, list[str]]:
    """プロファイルを適用する。set_default(id, role) / set_level(id, mute, volume) は Windows を操作する関数（テストでは差し替え）。

    戻り値は targets() と同じ（自動で戻す機能が使う）。見つからないデバイスは飛ばし、名前を返す。
    """
    tg, missing = targets(prof, devices)
    for (_flow, role), dev_id in tg.items():
        set_default(dev_id, ROLES[role])
    if with_levels:
        for lv in prof.get("levels", []):
            dev_id = resolve(lv, devices)
            if dev_id:
                set_level(dev_id, lv.get("mute"), lv.get("volume"))
    return tg, missing


# ================= Windows Core Audio（ctypes） =================
_ole32 = ctypes.OleDLL("ole32") if os.name == "nt" else None


class GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        g = cls()
        _ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(g))
        return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("ptr", c_void_p), ("pad", c_void_p)]


CLSCTX_ALL, STGM_READ, DEVICE_STATE_ACTIVE, VT_LPWSTR = 23, 0, 1, 31
_CLSID_ENUM, _IID_ENUM = "{BCDE0395-E52F-467C-8E3D-C4579291692E}", "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_CLSID_POLICY, _IID_POLICY = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}", "{F8679F50-850A-41CF-9C72-430F290290C8}"
_IID_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_PKEY_FRIENDLY = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)   # 「スピーカー (Realtek Audio)」
_PKEY_DESC = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 2)        # 「スピーカー」（SoundVolumeView の Name と同じ）


def _call(obj: c_void_p, index: int, argtypes: list, *args):
    """COM のメソッドを vtable の番号で呼ぶ。失敗した HRESULT は OSError になる。"""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtbl[index])(obj, *args)


def _release(obj: c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])(obj)


def _init() -> None:
    try:
        _ole32.CoInitializeEx(None, 2)   # STA。Qt の画面スレッドでは初期化済みなので失敗しても構わない
    except OSError:
        pass


def _create(clsid: str, iid: str) -> c_void_p:
    _init()
    p = c_void_p()
    _ole32.CoCreateInstance(byref(GUID.of(clsid)), None, CLSCTX_ALL, byref(GUID.of(iid)), byref(p))
    return p


def _prop(store: c_void_p, key: tuple) -> str:
    pk = PROPERTYKEY(GUID.of(key[0]), key[1])
    pv = PROPVARIANT()
    try:
        _call(store, 5, [POINTER(PROPERTYKEY), POINTER(PROPVARIANT)], byref(pk), byref(pv))
        return ctypes.wstring_at(pv.ptr) if pv.vt == VT_LPWSTR and pv.ptr else ""
    except OSError:
        return ""
    finally:
        _ole32.PropVariantClear(byref(pv))


def _device_id(dev: c_void_p) -> str:
    p = c_void_p()
    _call(dev, 5, [POINTER(c_void_p)], byref(p))
    try:
        return ctypes.wstring_at(p.value)
    finally:
        _ole32.CoTaskMemFree(p)


def _volume(dev: c_void_p) -> c_void_p:
    vol = c_void_p()
    _call(dev, 3, [POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)], byref(GUID.of(_IID_VOLUME)), CLSCTX_ALL, None, byref(vol))
    return vol


def list_devices() -> list[dict]:
    """使える（有効で接続されている）出力 / 入力デバイス。[{id, name, desc, flow, mute, volume}]"""
    out = []
    enum = _create(_CLSID_ENUM, _IID_ENUM)
    try:
        for flow, f in FLOWS.items():
            coll = c_void_p()
            _call(enum, 3, [ctypes.c_int, wintypes.DWORD, POINTER(c_void_p)], f, DEVICE_STATE_ACTIVE, byref(coll))
            try:
                n = wintypes.UINT()
                _call(coll, 3, [POINTER(wintypes.UINT)], byref(n))
                for i in range(n.value):
                    dev = c_void_p()
                    _call(coll, 4, [wintypes.UINT, POINTER(c_void_p)], i, byref(dev))
                    store, vol = c_void_p(), c_void_p()
                    try:
                        _call(dev, 4, [wintypes.DWORD, POINTER(c_void_p)], STGM_READ, byref(store))
                        d = {"id": _device_id(dev), "name": _prop(store, _PKEY_DESC), "desc": _prop(store, _PKEY_DESC),
                             "label": _prop(store, _PKEY_FRIENDLY), "flow": flow, "mute": None, "volume": None}
                        try:
                            vol = _volume(dev)
                            m, v = wintypes.BOOL(), c_float()
                            _call(vol, 15, [POINTER(wintypes.BOOL)], byref(m))
                            _call(vol, 9, [POINTER(c_float)], byref(v))
                            d["mute"], d["volume"] = bool(m.value), round(v.value, 4)
                        except OSError:
                            pass   # 音量を持たないデバイスもある
                        out.append(d)
                    finally:
                        _release(vol)
                        _release(store)
                        _release(dev)
            finally:
                _release(coll)
    finally:
        _release(enum)
    return out


def get_defaults() -> dict:
    """{"render" | "capture": {role: デバイス ID}}。デバイスが 1 つも無い向き / 役割は入らない。"""
    out = {"render": {}, "capture": {}}
    enum = _create(_CLSID_ENUM, _IID_ENUM)
    try:
        for flow, f in FLOWS.items():
            for role, r in ROLES.items():
                dev = c_void_p()
                try:
                    _call(enum, 4, [ctypes.c_int, ctypes.c_int, POINTER(c_void_p)], f, r, byref(dev))
                    out[flow][role] = _device_id(dev)
                except OSError:
                    pass
                finally:
                    _release(dev)
    finally:
        _release(enum)
    return out


def set_default(dev_id: str, role: int) -> None:
    pc = _create(_CLSID_POLICY, _IID_POLICY)
    try:
        _call(pc, 13, [wintypes.LPCWSTR, ctypes.c_int], dev_id, role)
    finally:
        _release(pc)


def set_level(dev_id: str, mute: bool | None, volume: float | None) -> None:
    enum = _create(_CLSID_ENUM, _IID_ENUM)
    dev, vol = c_void_p(), c_void_p()
    try:
        _call(enum, 5, [wintypes.LPCWSTR, POINTER(c_void_p)], dev_id, byref(dev))
        vol = _volume(dev)
        if volume is not None:
            _call(vol, 7, [c_float, c_void_p], min(max(float(volume), 0.0), 1.0), None)
        if mute is not None:
            _call(vol, 14, [wintypes.BOOL, c_void_p], bool(mute), None)
    finally:
        _release(vol)
        _release(dev)
        _release(enum)
