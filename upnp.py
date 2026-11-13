"""ルーターのポート開放（UPnP IGD の公開仕様どおり）。標準ライブラリだけで書く。

どの関数も通信を待つので、画面のスレッドからは呼ばないこと（設定画面は別スレッドで呼ぶ）。
開けられるのは「この PC 宛て」だけ。閉じられるのも「この PC 宛て」だけ。
"""
import ipaddress
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

SSDP_ADDR = ("239.255.255.250", 1900)
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
LEASE_SEC = 24 * 60 * 60   # 開けたポートはこの時間でルーターが自動で閉じる（閉め忘れ対策）
MAX_ENTRIES = 256          # 一覧を読むときの上限（壊れたルーターが終わりを返さなくても止まる）
DESCRIPTION = "DDLauncher"
# 外に開けると乗っ取りや覗き見につながる番号。開けようとしたら止める
DANGEROUS_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 135: "Windows RPC", 137: "NetBIOS", 138: "NetBIOS", 139: "NetBIOS",
    445: "Windows file sharing (SMB)", 1900: "UPnP", 3389: "Remote Desktop", 4444: "OBS WebSocket (v4)",
    4455: "OBS WebSocket", 5900: "VNC", 5938: "TeamViewer",
}


class UpnpError(Exception):
    """code はルーターが返した UPnP のエラー番号（無ければ 0）。"""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_ssdp(data: bytes) -> str | None:
    """M-SEARCH の応答 → 機器の説明 XML の URL。LAN 内（プライベート IP）の http だけ受け取る。"""
    m = re.search(rb"^location:\s*(\S+)", data, re.I | re.M)
    if not m:
        return None
    url = m.group(1).decode("ascii", "replace")
    parts = urlsplit(url)
    try:
        private = ipaddress.ip_address(parts.hostname or "").is_private
    except ValueError:
        private = False
    return url if parts.scheme == "http" and private else None


def find_control(xml_text: bytes, base_url: str) -> tuple[str, str] | None:
    """機器の説明 XML → (操作用 URL, サービスの種類)。WAN 側の接続サービスが無ければ None。"""
    root = ET.fromstring(xml_text)
    found = {}
    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in svc}
        if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
            found[fields["serviceType"]] = fields["controlURL"]
    base = next((e.text for e in root.iter() if _local(e.tag) == "URLBase" and e.text), base_url)
    for st in WAN_SERVICES:   # 新しい版を優先
        if st in found:
            return urljoin(base, found[st]), st
    return None


def device_info(xml_text: bytes) -> dict[str, str]:
    """機器の説明 XML → 一番外側の機器のメーカー・機種名など。"""
    dev = next((e for e in ET.fromstring(xml_text).iter() if _local(e.tag) == "device"), None)
    keys = ("friendlyName", "manufacturer", "modelName", "modelNumber")
    return {_local(c.tag): (c.text or "").strip() for c in dev if _local(c.tag) in keys} if dev is not None else {}


def parse_soap(xml_text: bytes) -> dict[str, str]:
    """SOAP の応答 → {要素名: 値}。エラーなら UpnpError（ルーターのエラー番号つき）。"""
    root = ET.fromstring(xml_text)
    fault = next((e for e in root.iter() if _local(e.tag) == "UPnPError"), None)
    if fault is not None:
        f = {_local(c.tag): (c.text or "").strip() for c in fault}
        raise UpnpError(f.get("errorDescription", "UPnP error"), int(f.get("errorCode", "0") or 0))
    body = next((e for e in root.iter() if _local(e.tag).endswith("Response")), None)
    return {_local(c.tag): (c.text or "").strip() for c in body} if body is not None else {}


class Router:
    def __init__(self, control_url: str, service: str, local_ip: str):
        self.control_url, self.service, self.local_ip = control_url, service, local_ip
        self.info: dict[str, str] = {}   # 機種名など（device_info の結果）

    def status(self) -> dict:
        """WAN 側の接続状態と稼働秒数。対応していないルーターもあるので、読めなければ空。"""
        try:
            r = self._call("GetStatusInfo")
        except (UpnpError, OSError, ET.ParseError):
            return {}
        return {"status": r.get("NewConnectionStatus", ""), "uptime": int(r.get("NewUptime", 0) or 0)}

    def _call(self, action: str, args: dict | None = None, timeout: float = 5) -> dict[str, str]:
        from xml.sax.saxutils import escape
        inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in (args or {}).items())
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{self.service}">{inner}</u:{action}></s:Body></s:Envelope>').encode()
        req = urllib.request.Request(self.control_url, body, {
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{self.service}#{action}"'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return parse_soap(r.read())
        except urllib.error.HTTPError as e:   # エラーは 500 + SOAP の本文で返ってくる
            return parse_soap(e.read())

    def external_ip(self) -> str:
        return self._call("GetExternalIPAddress").get("NewExternalIPAddress", "")

    def mappings(self) -> list[dict]:
        out = []
        for i in range(MAX_ENTRIES):
            try:
                r = self._call("GetGenericPortMappingEntry", {"NewPortMappingIndex": i})
            except UpnpError as e:
                if e.code in (713, 714) or i:   # 713 = 番号の範囲外 = 一覧の終わり
                    break
                raise
            out.append({"port": int(r.get("NewExternalPort", 0) or 0), "protocol": r.get("NewProtocol", ""),
                        "client": r.get("NewInternalClient", ""), "internal_port": int(r.get("NewInternalPort", 0) or 0),
                        "description": r.get("NewPortMappingDescription", ""),
                        "lease": int(r.get("NewLeaseDuration", 0) or 0)})
        return out

    def open(self, port: int, protocol: str) -> bool:
        """この PC 宛てに開ける。期限付きで登録できたら True、ルーターが期限なししか受け付けなければ False。"""
        check_port(port)
        args = {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": protocol, "NewInternalPort": port,
                "NewInternalClient": self.local_ip, "NewEnabled": 1, "NewPortMappingDescription": DESCRIPTION,
                "NewLeaseDuration": LEASE_SEC}
        try:
            self._call("AddPortMapping", args)
            return True
        except UpnpError as e:
            if e.code != 725:   # 725 = 期限なしの登録しか受け付けない
                raise
        self._call("AddPortMapping", dict(args, NewLeaseDuration=0))
        return False

    def close(self, port: int, protocol: str, client: str) -> None:
        if client != self.local_ip:
            raise UpnpError("not this PC")   # 画面でも止めるが、念のためここでも
        self._call("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": protocol})


def check_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise UpnpError(f"port {port}")
    if port in DANGEROUS_PORTS:
        raise UpnpError(DANGEROUS_PORTS[port], -1)   # -1 = こちらで止めた


def discover(timeout: float = 3.0) -> Router:
    """LAN 内のルーターを探す。見つからなければ UpnpError。"""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, SSDP_ADDR)
        seen = set()
        while True:   # タイムアウトで抜ける。返事をくれた機器を順に試す
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            url = parse_ssdp(data)
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    xml_text = r.read()
                ctl = find_control(xml_text, url)
            except (OSError, ET.ParseError):
                continue
            if ctl:
                router = Router(ctl[0], ctl[1], _local_ip_toward(urlsplit(url).hostname))
                router.info = device_info(xml_text)
                return router
    finally:
        sock.close()
    raise UpnpError("no router")


def _local_ip_toward(host: str) -> str:
    """ルーターへ届くときに使う、この PC の LAN 側 IP。UDP の connect は実際には何も送らない。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 1900))
        return s.getsockname()[0]
