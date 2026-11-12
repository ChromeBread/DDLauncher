"""見た目: 暗い箱、細いシアンの線、角ばった英字。色・フォントはここに集約。"""

C = {
    "bg": "#0b1215",
    "bg_glass": "rgba(11, 18, 21, 150)",
    "tile": "#101c21",
    "tile_glass": "rgba(16, 28, 33, 170)",
    "tab": "#1b2a2f",          # 見出しタブ
    "line": "#24414a",
    "text": "#c8e6e0",
    "dim": "#5f8282",
    "accent": "#5fe0d0",       # 起動中（通常権限）・強調色
    "warn": "#e8892c",         # 起動中（管理者）・次に起動（選択中のオレンジ）
    "err": "#e05050",          # 管理者指定なのに通常権限で動いている等
    "off": "#2a3638",          # 停止
    "obs_idle": "#6fd3ff",     # OBS: 配信していない（水色の縁）
    "obs_live": "#ff3b3b",     # OBS: 配信中（赤の縁）
    "obs_rec": "#b070ff",      # OBS: 録画のみ（紫の縁）
}
DEFAULT_C = dict(C)
# カスタムで変えられる色（*_glass は bg / tile から作る）。並びは設定画面の並び
CUSTOM_KEYS = ["bg", "tile", "tab", "line", "text", "dim", "accent", "warn", "err", "off", "obs_idle", "obs_live", "obs_rec"]

# 配色テンプレート（設定画面で選ぶと custom の色に丸ごと入る）。並びは CUSTOM_KEYS と同じ
TEMPLATES = {
    "amber": dict(zip(CUSTOM_KEYS, "#14100b #1e1710 #2b2117 #4a3a26 #f0e0c8 #8a7458 #ffb347 #ff6f3c #e04848 #33291f #6fd3ff #ff3b3b #b070ff".split())),
    "mono": dict(zip(CUSTOM_KEYS, "#111111 #1a1a1a #262626 #3d3d3d #e6e6e6 #808080 #f5f5f5 #ffb000 #ff4d4d #2e2e2e #9ad7ff #ff3b3b #c28cff".split())),
    "sakura": dict(zip(CUSTOM_KEYS, "#1a1016 #24161f #33202c #5a3550 #f6dbe8 #9a7088 #ff8fc7 #ffb35c #ff5a6e #3a2a33 #8fd8ff #ff3b3b #b98cff".split())),
    "forest": dict(zip(CUSTOM_KEYS, "#0d140e #132016 #1c2e20 #2f4d35 #d5ecd8 #6c8c72 #7fe08a #f0a03c #e05a50 #26332a #6fd3ff #ff3b3b #b070ff".split())),
    "light": dict(zip(CUSTOM_KEYS, "#eef2f3 #ffffff #d9e2e4 #a9bcc0 #1d2b2f #6c8287 #0f9d8a #d9730d #c62828 #c9d3d5 #1e88e5 #e53935 #8e44ad".split())),
    "contrast": dict(zip(CUSTOM_KEYS, "#000000 #000000 #1a1a1a #ffffff #ffffff #c0c0c0 #00ff66 #ffcc00 #ff3030 #555555 #00ccff #ff0000 #cc66ff".split())),
}


def apply_colors(custom: dict | None, opacity: int = 100) -> None:
    """C を既定に戻し、custom（キー → "#rrggbb"）で上書きする。C は各所が直接読むので中身だけ入れ替える。

    opacity（20〜100 %）は背景とタイルの濃さ。100 なら不透明と同じ見た目。
    """
    C.clear()
    C.update(DEFAULT_C)
    C.update({k: v for k, v in (custom or {}).items() if k in CUSTOM_KEYS})
    a = round(255 * min(max(opacity, 0), 100) / 100)
    for glass, base, alpha in (("bg_glass", "bg", a), ("tile_glass", "tile", min(255, a + 20))):   # タイルは背景より少し濃く
        r, g, b = (int(C[base][i:i + 2], 16) for i in (1, 3, 5))
        C[glass] = f"rgba({r}, {g}, {b}, {alpha})"

FONT = "Bahnschrift SemiBold, Consolas, 'Meiryo UI', 'Malgun Gothic', 'Microsoft JhengHei UI'"   # Bahnschrift は Win10 以降標準。角ばった英字

BASE_TILE = (96, 58)    # scale=1.0 のタイルサイズ(px)。中央アイコン + 下段名前の比率


def app_icon():
    """トレイ / タスクバー用アイコン（画像ファイルを同梱しないよう描画で作る）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    pm = QPixmap(32, 32)
    pm.fill(QColor(C["bg"]))
    p = QPainter(pm)
    p.setPen(QColor(C["accent"]))
    p.drawRect(0, 0, 31, 31)
    f = QFont("Bahnschrift SemiBold")
    f.setPixelSize(16)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "DD")
    p.end()
    return QIcon(pm)


def qcolor(value: str):
    """C の色 → QColor。QColor は "rgba(r, g, b, a)" を読めない（無効 = 黒になる）ので自分で分ける。"""
    from PySide6.QtGui import QColor
    if value.startswith("rgba("):
        r, g, b, a = (int(x) for x in value[5:-1].split(","))
        return QColor(r, g, b, a)
    return QColor(value)


def px(n: float, scale: float) -> int:
    return max(1, round(n * scale))


def stylesheet(scale: float = 1.0) -> str:
    bg = C["bg_glass"]   # 濃さは apply_colors の opacity で決まる
    f = px(11, scale)
    return f"""
    #Root {{ background: {bg}; border: 1px solid {C['line']}; }}
    #Root[dnd="true"] {{ border: 1px solid {C['accent']}; }}
    QLabel, QToolButton {{ color: {C['text']}; font-family: {FONT}; font-size: {f}px; }}
    #Status {{ background: {C['tab']}; color: {C['dim']}; padding: 0 {px(6, scale)}px; }}
    #Warn {{ color: {C['warn']}; }}
    QToolButton {{ background: transparent; border: 1px solid transparent; padding: 0 {px(4, scale)}px; }}
    QToolButton:hover {{ border: 1px solid {C['line']}; }}
    QToolButton:checked {{ color: {C['bg']}; background: {C['accent']}; }}
    QMenu {{ background: {C['bg']}; color: {C['text']}; border: 1px solid {C['line']};
             font-family: {FONT}; font-size: 12px; }}
    QMenu::item:selected {{ background: {C['line']}; }}
    """
