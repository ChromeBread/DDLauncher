"""Build date and where things live (shown on the Info tab)."""
import datetime
import glob
import json
import os
import sys

import config

VERSION = "0.3.1"
DEVELOPER = "ChromeBread feat. BlackDream"

FROZEN = getattr(sys, "frozen", False)   # running as the PyInstaller exe
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

_MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "id": ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
           "Agustus", "September", "Oktober", "November", "Desember"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"],
}


def build_date() -> datetime.date:
    """The exe has build_info.json written by DDLauncher.spec. From source, use the newest .py file's date."""
    try:
        with open(os.path.join(_BASE, "build_info.json"), encoding="utf-8") as f:
            return datetime.date.fromisoformat(json.load(f)["build_date"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    files = glob.glob(os.path.join(_BASE, "*.py"))
    newest = max((os.path.getmtime(p) for p in files), default=0)
    return datetime.date.fromtimestamp(newest) if newest else datetime.date.today()


def format_date(d: datetime.date, lang: str) -> str:
    """Each language's natural written form, e.g. 2012年1月12日 / January 12, 2012."""
    y, m, day = d.year, d.month, d.day
    if lang in ("ja", "zh_TW"):
        return f"{y}年{m}月{day}日"
    if lang == "ko":
        return f"{y}년 {m}월 {day}일"
    if lang == "id":
        return f"{day} {_MONTHS['id'][m - 1]} {y}"
    if lang == "fr":
        return f"{'1er' if day == 1 else day} {_MONTHS['fr'][m - 1]} {y}"
    return f"{_MONTHS['en'][m - 1]} {day}, {y}"


def app_location() -> str:
    """The exe file itself, or the source folder when run from Python."""
    return sys.executable if FROZEN else os.path.dirname(os.path.abspath(__file__))


def legacy_exists() -> bool:
    return os.path.isdir(config.LEGACY_DIR)
