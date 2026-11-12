"""URL を登録したタイル用に、サイトのアイコン（/favicon.ico）か、画像の URL ならその画像を取ってきて保存する。"""
import hashlib
import os
import threading
import urllib.request
from urllib.parse import urlsplit

import config

CACHE_DIR = os.path.join(config.CONFIG_DIR, "favicons")
MAX_BYTES = 512 * 1024
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".ico", ".gif", ".bmp", ".webp")


def is_url(path: str) -> bool:
    return urlsplit(path).scheme in ("http", "https")


def is_image_url(url: str) -> bool:
    return is_url(url) and urlsplit(url).path.lower().endswith(IMAGE_EXT)


def cache_path(url: str) -> str:
    """サイトのアイコンはサイトごとに 1 つ、画像の URL は URL ごとに 1 つ保存する。"""
    key = url if is_image_url(url) else urlsplit(url).netloc.lower()
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest()[:16] + ".ico")


def fetch(url: str, done) -> None:
    """別スレッドで取得し、保存できたら done() を呼ぶ（失敗時は何もしない = 頭文字表示のまま）。"""
    def work():
        u = urlsplit(url)
        labels = u.netloc.split(".")
        hosts = [u.netloc] + ([".".join(labels[1:])] if len(labels) > 2 else [])   # live.example.jp に無ければ example.jp
        urls = [url] if is_image_url(url) else [f"{u.scheme}://{host}/favicon.ico" for host in hosts]
        data = None
        for src in urls:
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "DDLauncher"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = r.read(MAX_BYTES + 1)
                break
            except Exception:   # 404 / オフライン → 次の候補
                continue
        try:
            if not data or len(data) > MAX_BYTES:
                return
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = cache_path(url) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, cache_path(url))
            done()
        except Exception:   # オフライン / favicon 無し / 書き込み不可
            pass
    threading.Thread(target=work, daemon=True).start()
