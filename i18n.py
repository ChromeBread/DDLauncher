"""UI language. Strings live in locales/<code>.json, help pages in locales/help/<code>.html.

A key missing from the current language falls back to English, then to the key itself,
so a half-finished translation never shows an empty label.
"""
import configparser
import ctypes
import json
import locale
import os
import sys

# Shown in the Language pulldown in each language's own name
LANGS = {
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "id": "Bahasa Indonesia",
    "fr": "Français",
    "zh_TW": "繁體中文",
}
FALLBACK = "en"

_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # PyInstaller unpacks here
LOCALE_DIR = os.path.join(_BASE, "locales")

_current = FALLBACK
_tables: dict[str, dict] = {}


def _table(code: str) -> dict:
    if code not in _tables:
        try:
            with open(os.path.join(LOCALE_DIR, f"{code}.json"), encoding="utf-8") as f:
                _tables[code] = json.load(f)
        except (OSError, ValueError):
            _tables[code] = {}
    return _tables[code]


def from_tag(tag: str) -> str | None:
    """'ja-JP' / 'ja_JP' / 'zh-TW' -> supported code, or None."""
    tag = (tag or "").replace("-", "_").strip()
    low = tag.lower()
    if low.startswith(("zh_tw", "zh_hk", "zh_hant")):
        return "zh_TW"
    if low.startswith("zh"):
        return None   # Simplified Chinese is not supported -> caller falls back to English
    head = low.split("_")[0]
    if head == "in":   # old Java/Windows code for Indonesian
        head = "id"
    return head if head in LANGS else None


def _obs_language() -> str | None:
    """OBS's own UI language ([General] Language in user.ini / global.ini), or None if OBS isn't set up."""
    root = os.path.join(os.environ.get("APPDATA", ""), "obs-studio")
    for name in ("user.ini", "global.ini"):   # OBS 31+ keeps it in user.ini
        p = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            p.read(os.path.join(root, name), encoding="utf-8-sig")
        except (OSError, configparser.Error):
            continue
        tag = p.get("General", "Language", fallback="")
        if tag:
            return tag
    return None


def _windows_language() -> str:
    try:
        return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
    except (AttributeError, OSError):
        return ""


def detect() -> str:
    """Follow OBS's language; if OBS isn't installed, Windows'. Anything unsupported -> English."""
    obs = _obs_language()
    if obs is not None:
        return from_tag(obs) or FALLBACK
    return from_tag(_windows_language()) or FALLBACK


def resolve(setting: str) -> str:
    return setting if setting in LANGS else detect()


def set_language(setting: str) -> str:
    """setting: a code from LANGS, or 'auto'. Returns the code actually used."""
    global _current
    _current = resolve(setting)
    return _current


def current() -> str:
    return _current


def tr(key: str, **kw) -> str:
    text = _table(_current).get(key)
    if text is None:
        text = _table(FALLBACK).get(key, key)
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def help_html() -> str:
    for code in (_current, FALLBACK):
        try:
            with open(os.path.join(LOCALE_DIR, "help", f"{code}.html"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return ""
