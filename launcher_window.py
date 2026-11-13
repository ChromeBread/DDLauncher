"""ランチャーウィンドウ本体（1 インスタンス = 1 ランチャー。複数配置はこれを複数作るだけ）。"""
import math
import os
import time

from PySide6.QtCore import (QAbstractAnimation, QEvent, QFileInfo, QPoint, QPropertyAnimation, QRect, QSize,
                            Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileIconProvider, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QMenu, QMessageBox, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

import config
import favicon
import i18n
import theme
import winproc
from obs_bridge import ObsBridge, base_name, collect_files, file_kind, fmt_bytes, name_conflicts, source_spec
from sequencer import Sequencer, place_on_monitor

tr = i18n.tr

SCALE_RANGE = (0.5, 2.5)
CONFIRM_WAIT_SEC = 60   # シーケンス完了後、全アプリの起動を待つ最大秒数
FLYOUT_GRACE_SEC = 0.8   # マウスがランチャーから外れても横の列を閉じずに待つ秒数
ANIM_INTERVAL_MS = {"full": 33, "reduced": 250, "off": 0}   # Reduced = 4fps の点滅のみ


def corner_pos(area: tuple[int, int, int, int], size: tuple[int, int], corner: str, margin: int) -> tuple[int, int]:
    """作業領域 area=(x,y,w,h) の corner に size を置いた左上座標。必ず area 内に収める。"""
    ax, ay, aw, ah = area
    w, h = size
    x = ax + margin if "left" in corner else ax + aw - w - margin
    y = ay + margin if "top" in corner else ay + ah - h - margin
    return max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h))


def beside_rect(area: tuple[int, int, int, int], avoid: tuple[int, int, int, int],
                size: tuple[int, int], gap: int = 8) -> tuple[int, int, int, int]:
    """avoid（ランチャー）に被らない位置に size の窓を置く (x, y, w, h)。

    上・下・左・右の空き領域のうち丸ごと入る最初の所。どこにも入らなければ一番広く取れる所に縮めて入れる。
    """
    ax, ay, aw, ah = area
    lx, ly, lw, lh = avoid
    w, h = size
    regions = [(ax, ay, aw, ly - gap - ay),                           # 上
               (ax, ly + lh + gap, aw, ay + ah - (ly + lh + gap)),    # 下
               (ax, ay, lx - gap - ax, ah),                           # 左
               (lx + lw + gap, ay, ax + aw - (lx + lw + gap), ah)]    # 右
    fits = [r for r in regions if r[2] >= w and r[3] >= h]
    rx, ry, rw, rh = fits[0] if fits else max(regions, key=lambda r: min(r[2] / w, r[3] / h))
    w, h = min(w, rw), min(h, rh)
    x = max(rx, min(lx + lw // 2 - w // 2, rx + rw - w))   # ランチャー寄りに置く
    y = max(ry, min(ly + lh // 2 - h // 2, ry + rh - h))
    return x, y, w, h


def l_cells(n: int, max_cols: int, max_rows: int) -> list[tuple[int, int]]:
    """LINEAR の配置。角で折れる L 字（0 = 無制限）。

    返り値は (r, c) = (角からの段数, 角からの列数)。画面の向きへの反転は呼び出し側で行う。
    1 番目のアプリが角に入り、続きが角から画面の内側へ横に並ぶ（横の上限まで）。
    残りは角の上に積み上がり、最後のアプリが角のすぐ上に来る。上から読むと
    「… 最後 → 1 番目（角）→ 2 番目 …」と、縦の列が角で曲がって横の列につながる。
    L 字に入り切らなければ、1 マス内側にもう 1 つ L 字を作る。
    横 × 縦 にも入り切らなければ横に広げる。
    """
    if n <= 0:
        return []
    w, h = max_cols, max_rows
    if w <= 0 and h <= 0:
        h = n // 2 + 1                       # 両方無制限: 縦と横をほぼ半分ずつ
    if w <= 0:
        w = max(1, n - h + 1)
    elif h <= 0:
        h = max(1, n - w + 1)
    if w * h < n:
        w = math.ceil(n / h)
    cells: list[tuple[int, int]] = []
    k = 0
    while len(cells) < n:
        left = n - len(cells)
        row = [(k, c) for c in range(k, w)][:left]                     # 角 → 内側へ
        col = [(r, k) for r in range(k + 1, h)][:left - len(row)]      # 角の上（近い方が後）
        cells += row + col[::-1]
        k += 1
    return cells


def group_cells(n: int, max_cols: int, max_rows: int) -> list[tuple[int, int]]:
    """GROUP の配置（0 = 無制限）。角から縦に並べ、縦の上限で区切って内側に次の列を作る。

    各列の中の並びは縦一列のときと同じ（列の 1 番目が角の段、残りは l_cells の縦と同じ順）。
    横の上限を超える列数になるときは、横を上限に抑えて縦を伸ばす。
    """
    if n <= 0:
        return []
    rows = max_rows if max_rows > 0 else n
    if max_cols > 0 and math.ceil(n / rows) > max_cols:
        rows = math.ceil(n / max_cols)
    cells: list[tuple[int, int]] = []
    for c, start in enumerate(range(0, n, rows)):
        cells += [(r, c) for r, _ in l_cells(min(rows, n - start), 1, 0)]
    return cells


def linear_slots(apps: list[dict]) -> list[list[int]]:
    """LINEAR の L 字に並ぶ枠。同じカテゴリのアプリは最初の 1 個の後ろに重ねる（空欄は 1 個ずつ）。"""
    slots, by_group = [], {}
    for i, a in enumerate(apps):
        g = a.get("group")
        if g and g in by_group:
            by_group[g].append(i)
        else:
            slots.append([i])
            if g:
                by_group[g] = slots[-1]
    return slots


def load_app_icon(app: dict, on_fetched=None) -> QIcon | None:
    """カスタム画像 > Web のアイコン（アイコン欄かパスが URL のとき）> exe のアイコン。on_fetched があれば Web から取りに行く。

    アイコン欄の URL が取れないあいだは exe のアイコンを出す。
    """
    custom, path = app.get("icon", ""), app.get("path", "")
    if custom and not favicon.is_url(custom) and os.path.exists(custom):
        pm = QPixmap(custom)
        return None if pm.isNull() else QIcon(pm)
    web = custom if favicon.is_url(custom) else path if favicon.is_url(path) else ""
    if web:
        cached = favicon.cache_path(web)
        pm = QPixmap(cached) if os.path.exists(cached) else QPixmap()
        if not pm.isNull():
            return QIcon(pm)
        if on_fetched:   # 未取得 / 画像でなかった（404 ページ等）→ 取り直す
            favicon.fetch(web, on_fetched)
    if path and os.path.exists(path):
        return QFileIconProvider().icon(QFileInfo(path))
    return None


class AppTile(QWidget):
    """タイル: 中央にアイコン、その右下に被せて状態、下段に名前、右端に状態ランプ。"""
    launch_requested = Signal(int, object, bool)   # (index, admin: bool|None, クリックか)
    hovered = Signal(object)
    icon_ready = Signal()   # サイトのアイコンを別スレッドで取得し終えた
    members: list = []   # 重ねて隠しているタイル（マウスオーバーで横に出す）

    def __init__(self, index: int, app: dict, scale: float, transparent: bool, parent=None):
        super().__init__(parent)
        self.index, self.app = index, app
        self.running, self.elevated = False, None
        self.obs = None   # OBS のタイルだけ: 配信 / 録画状態
        self.is_obs = os.path.basename(app.get("path", "")).lower() in ("obs64.exe", "obs32.exe", "obs.exe")
        self.highlight = 0.0
        self.bg = theme.qcolor(theme.C["tile_glass"] if transparent else theme.C["tile"])
        self.set_scale(scale)
        self.icon_ready.connect(self._load_icon)
        self._load_icon(fetch=True)
        self._update_tooltip()

    def _load_icon(self, fetch: bool = False) -> None:
        self.icon = load_app_icon(self.app, self.icon_ready.emit if fetch else None)
        self.update()

    def set_scale(self, s: float) -> None:
        self.s = s
        self.setFixedSize(theme.px(theme.BASE_TILE[0], s), theme.px(theme.BASE_TILE[1], s))
        self.name_font = QFont("Bahnschrift SemiBold")
        self.name_font.setPixelSize(theme.px(13, s))
        self.small_font = QFont("Bahnschrift")
        self.small_font.setPixelSize(theme.px(10, s))
        self.ammo_font = QFont("Bahnschrift SemiBold")
        self.ammo_font.setPixelSize(theme.px(11, s))
        self.update()

    def set_state(self, running: bool, elevated: bool | None) -> None:
        if (running, elevated) != (self.running, self.elevated):
            self.running, self.elevated = running, elevated
            self._update_tooltip()
            self.update()

    def set_obs(self, state) -> None:
        if self.is_obs and state != self.obs:
            self.obs = state
            self.update()

    def _color(self) -> str:
        if not self.running:
            return theme.C["off"]
        if self.app.get("admin") and self.elevated is False:
            return theme.C["err"]
        return theme.C["warn"] if self.elevated else theme.C["accent"]

    def _update_tooltip(self) -> None:
        a = self.app
        if not a.get("path"):
            st = tr("tile.tip.no_path")
        elif not self.running:
            st = tr("tile.tip.stopped")
        else:
            st = tr({True: "tile.tip.running_admin", False: "tile.tip.running_user",
                     None: "tile.tip.running_unknown"}[self.elevated])
        flags = " ".join(f for f, on in (("ADMIN", a.get("admin")), ("MIN", a.get("minimized"))) if on)
        self.setToolTip(f"{a['name']}\n{st}\n{flags}".strip())

    def _state_text(self) -> str:
        if not self.app.get("path"):
            return "NO PATH"
        if not self.running:
            return "STANDBY"
        return {True: "ADMIN", False: "USER", None: "RUN"}[self.elevated]

    def paintEvent(self, _):
        p = QPainter(self)
        s = self.s
        col = QColor(self._color())
        w, h = self.width(), self.height()
        lamp_w = theme.px(5, s)
        box = QRect(0, 0, w - lamp_w - theme.px(3, s), h)   # 右に独立したランプ列
        p.fillRect(box, self.bg)
        if self.highlight > 0:   # 次に起動するタイル: 選択中のオレンジ
            hl = QColor(theme.C["warn"])
            hl.setAlphaF(0.45 * self.highlight)
            p.fillRect(box, hl)
        if self.obs:   # OBS: 配信中 = 赤 / 録画のみ = 紫 / どちらでもない = 水色
            edge = QColor(theme.C["obs_live" if self.obs["stream"] else "obs_rec" if self.obs["record"] else "obs_idle"])
            bw = theme.px(2, s)
            p.setPen(QPen(edge, bw))
            p.drawRect(box.adjusted(bw // 2, bw // 2, -(bw + 1) // 2, -(bw + 1) // 2))
        else:
            p.setPen(col if self.running else QColor(theme.C["line"]))
            p.drawRect(box.adjusted(0, 0, -1, -1))
        p.fillRect(w - lamp_w, h - theme.px(14, s), lamp_w, theme.px(14, s), col)

        # 中央にアイコン、その右下に被せて状態 / 通信量、下段に名前
        pad = theme.px(5, s)
        name_h = QFontMetrics(self.name_font).height()
        icon_sz = min(theme.px(30, s), h - name_h - pad)
        icon_rect = QRect(box.center().x() - icon_sz // 2, pad, icon_sz, icon_sz)
        p.setOpacity(1.0 if self.running else 0.45)
        if self.icon:   # 16px の favicon も枠いっぱいに拡大（QIcon.paint は拡大しない）
            pm = self.icon.pixmap(256).scaled(icon_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(icon_rect.center().x() - pm.width() // 2, icon_rect.center().y() - pm.height() // 2, pm)
        else:  # アイコンが取れない（未設定 / 取得中）なら頭文字
            p.setPen(QColor(theme.C["dim"]))
            p.setFont(self.name_font)
            p.drawText(icon_rect, Qt.AlignCenter, (self.app.get("name") or "?")[:1])
        p.setOpacity(1.0)

        text_rect = box.adjusted(pad, pad, -pad, -theme.px(2, s))
        ammo_rect = QRect(text_rect.left(), icon_rect.top(), text_rect.width(), icon_rect.height())
        p.setFont(self.ammo_font)
        if self.obs and (self.obs["stream"] or self.obs["record"]):   # アイコン右下に通信量
            p.setPen(QColor(theme.C["text"]))
            ammo = fmt_bytes(self.obs["bytes"])
        else:
            p.setPen(col if self.running else QColor(theme.C["dim"]))
            ammo = self._state_text()
        p.drawText(ammo_rect, Qt.AlignRight | Qt.AlignBottom, ammo)
        p.setFont(self.name_font)
        p.setPen(QColor(theme.C["text"] if self.running else theme.C["dim"]))
        name = QFontMetrics(self.name_font).elidedText(self.app.get("name", "").upper(), Qt.ElideRight, text_rect.width())
        p.drawText(text_rect, Qt.AlignRight | Qt.AlignBottom, name)

    def enterEvent(self, e):
        self.hovered.emit(self)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.launch_requested.emit(self.index, None, True)

    def contextMenuEvent(self, e):
        m = QMenu(self)   # メニューから選ぶのは押し間違いではないので、OBS 起動中でも開く
        m.addAction(tr("tile.menu.launch"), lambda: self.launch_requested.emit(self.index, None, False))
        m.addAction(tr("tile.menu.launch_admin"), lambda: self.launch_requested.emit(self.index, True, False))
        m.addAction(tr("tile.menu.launch_user"), lambda: self.launch_requested.emit(self.index, False, False))
        m.exec(e.globalPos())


class MiniIcon(QWidget):
    """畳んだときに出すアイコン 1 個。マウスを乗せる（またはクリックする）と展開する。"""
    entered = Signal()

    def __init__(self, transparent: bool, parent=None):
        super().__init__(parent)
        self.bg = theme.qcolor(theme.C["tile_glass"] if transparent else theme.C["tile"])
        self.app, self.icon = None, None
        self.set_scale(1.0)

    def set_app(self, app: dict | None) -> None:
        """None なら DDLauncher の DD。アプリのアイコンが読めないときも DD にする。"""
        self.app = app
        self.reload()

    def reload(self) -> None:
        self.icon = load_app_icon(self.app) if self.app else None
        self.update()

    def set_scale(self, s: float) -> None:
        self.s = s
        side = theme.px(theme.BASE_TILE[1], s)   # タイルの高さと同じ正方形（小さすぎて狙いにくくならないように）
        self.setFixedSize(side, side)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), self.bg)
        p.setPen(QColor(theme.C["accent"]))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if self.icon:
            side = theme.px(32, self.s)
            r = self.rect()
            self.icon.paint(p, r.x() + (r.width() - side) // 2, r.y() + (r.height() - side) // 2, side, side)
            return
        f = QFont("Bahnschrift SemiBold")
        f.setPixelSize(theme.px(22, self.s))
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "DD")

    def enterEvent(self, e):
        self.entered.emit()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.entered.emit()


def free_rect(area: tuple[int, int, int, int], pos: tuple[int, int], size: tuple[int, int],
              avoid: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """動かした D&D スペースを area（その画面）に収めた (x, y, w, h)。

    ランチャーに被るなら、上下左右のうち一番近い辺にくっつく位置へずらす。画面内のどこにも置けなければ None（隣へ戻す）。
    """
    ax, ay, aw, ah = area
    w, h = min(size[0], aw), min(size[1], ah)
    x, y = max(ax, min(pos[0], ax + aw - w)), max(ay, min(pos[1], ay + ah - h))
    lx, ly, lw, lh = avoid
    if not (x < lx + lw and lx < x + w and y < ly + lh and ly < y + h):
        return x, y, w, h
    moves = [(abs(ly - h - y), x, ly - h),      # 上
             (abs(ly + lh - y), x, ly + lh),    # 下
             (abs(lx - w - x), lx - w, y),      # 左
             (abs(lx + lw - x), lx + lw, y)]    # 右
    ok = [m for m in moves if ax <= m[1] <= ax + aw - w and ay <= m[2] <= ay + ah - h]
    if not ok:
        return None
    _, x, y = min(ok)
    return x, y, w, h


GRIP_CORNERS = [Qt.TopEdge | Qt.LeftEdge, Qt.TopEdge | Qt.RightEdge, Qt.BottomEdge | Qt.LeftEdge, Qt.BottomEdge | Qt.RightEdge]
GRIP_SIDES = [Qt.TopEdge, Qt.BottomEdge, Qt.LeftEdge, Qt.RightEdge]


def grip_rect(edges, w: int, h: int, g: int, t: int) -> tuple[int, int, int, int]:
    """つまみの位置 (x, y, w, h)。角は g×g の正方形、辺は角を除いた太さ t の帯。"""
    top, bottom = bool(edges & Qt.TopEdge), bool(edges & Qt.BottomEdge)
    left, right = bool(edges & Qt.LeftEdge), bool(edges & Qt.RightEdge)
    if (top or bottom) and (left or right):   # 角
        return (0 if left else w - g), (0 if top else h - g), g, g
    if top or bottom:
        return g, (0 if top else h - t), max(w - 2 * g, 0), t
    return (0 if left else w - t), g, t, max(h - 2 * g, 0)


class ResizeGrip(QWidget):
    """D&D スペースの大きさを変えるつまみ（角または辺）。ドラッグは Windows 標準のサイズ変更に任せる。"""

    def __init__(self, panel: "DropPanel", edges):
        super().__init__(panel)
        self.panel = panel
        self.edges = edges
        top, left = bool(edges & Qt.TopEdge), bool(edges & Qt.LeftEdge)
        vertical, horizontal = bool(edges & (Qt.TopEdge | Qt.BottomEdge)), bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        self.corner = vertical and horizontal
        if self.corner:
            self.setCursor(Qt.SizeFDiagCursor if top == left else Qt.SizeBDiagCursor)
        else:
            self.setCursor(Qt.SizeVerCursor if vertical else Qt.SizeHorCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.window().windowHandle():
            self.panel.resizing = True
            self.panel.press_geo = self.panel.geometry()
            self.panel.resize_done.start()
            self.window().windowHandle().startSystemResize(self.edges)

    def paintEvent(self, _):
        if not self.corner:
            return   # 辺のつまみは描かない（マウスを乗せるとカーソルが変わる）
        p = QPainter(self)
        p.setPen(QPen(QColor(theme.C["accent"]), 1))
        w, h = self.width(), self.height()
        top, left = bool(self.edges & Qt.TopEdge), bool(self.edges & Qt.LeftEdge)
        cx, cy = (0 if left else w), (0 if top else h)   # つまみの角
        dx, dy = (1 if left else -1), (1 if top else -1)
        for i in (4, 8, 12):   # 角を囲む斜線 3 本
            p.drawLine(cx + dx * i, cy, cx, cy + dy * i)


class DropPanel(QWidget):
    """D&D モード中にランチャーの隣（くっつけるが被らない位置）に出すドロップ先。角のつまみで大きさを変えられる。"""

    def __init__(self, launcher: "LauncherWindow"):
        # 親を持たせない: Windows では持ち主の窓が最前面だと従う窓も最前面になり、別々に ON/OFF できないため
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, launcher.lcfg["dnd_on_top"])
        self.launcher = launcher
        self.resizing = False
        self.moving = False
        self.press_geo = QRect()
        self.want = QSize(0, 0)
        self.setCursor(Qt.SizeAllCursor)   # どこをつかんでも動かせる（つまみの所は大きさ変更）
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.root = QFrame(self, objectName="Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        lay = QVBoxLayout(self.root)
        self.title = QLabel("DRAG & DROP", objectName="PanelTitle")
        self.body = QLabel(objectName="PanelBody", wordWrap=True)
        tab = QHBoxLayout()
        tab.addWidget(QLabel("D&&D", objectName="PanelTab"))   # 左下の見出しタブ
        tab.addStretch()
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        lay.addStretch()
        lay.addLayout(tab)
        self.grips = [ResizeGrip(self, e) for e in GRIP_CORNERS + GRIP_SIDES]
        self._grip_size = (14, 5)   # (角の大きさ, 辺の太さ)。refresh で倍率に合わせる
        # サイズ変更の終わり（マウスを離した）を、リサイズが 0.3 秒止まったことで判定する
        self.resize_done = QTimer(self, singleShot=True, interval=300, timeout=self._on_resized)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.windowHandle():
            self.moving = True
            self.press_geo = self.geometry()
            self.resize_done.start()   # 動かさずに離した（ただのクリック）ときも後始末する
            self.windowHandle().startSystemMove()

    def moveEvent(self, e):
        super().moveEvent(e)
        if self.moving:
            self.resize_done.start()

    def refresh(self, s: float) -> None:
        scene = self.launcher.cfg["obs"]["target_scene"] or tr("scene.current")
        self.body.setText(tr("drop.body", scene=scene))
        c = theme.C
        self.root.setStyleSheet(f"""
            #Panel {{ background: {c['bg_glass']}; border: 1px solid {c['accent']}; }}
            QLabel {{ color: {c['text']}; font-family: {theme.FONT}; }}
            #PanelTitle {{ font-size: {theme.px(20, s)}px; }}
            #PanelBody {{ font-size: {theme.px(13, s)}px; }}
            #PanelTab {{ background: {c['tab']}; color: {c['accent']}; font-size: {theme.px(11, s)}px;
                         padding: 0 {theme.px(8, s)}px; }}""")
        m = theme.px(12, s)
        self.root.layout().setContentsMargins(m, m, m, theme.px(4, s))
        self._grip_size = (theme.px(14, s), max(theme.px(5, s), 4))
        # 最小 = 本文が収まる大きさ。既定 = 以前の大きさ（幅 380）の縦横 2 倍
        min_w = theme.px(240, s)
        self.setMinimumSize(min_w, self.layout().totalHeightForWidth(min_w))
        base_w = theme.px(380, s)
        w, h = self.launcher.lcfg["dnd_size"]
        if w and h:
            w, h = theme.px(w, s), theme.px(h, s)
        else:
            w, h = base_w * 2, self.layout().totalHeightForWidth(base_w) * 2
        self.want = QSize(max(w, self.minimumWidth()), max(h, self.minimumHeight()))
        self.anchor()

    def anchor(self) -> None:
        """動かした位置があればそこへ、無ければランチャーに隙間 0 でくっつける。どちらもランチャーには被せない。"""
        L = self.launcher
        launcher = (L.x(), L.y(), L.width(), L.height())
        size = (self.want.width(), self.want.height())
        rect = None
        pos = L.lcfg["dnd_pos"]
        scr = QGuiApplication.screenAt(QPoint(*pos)) if pos else None   # 外したモニタ上なら隣へ戻す
        if scr:
            a = scr.availableGeometry()
            rect = free_rect((a.x(), a.y(), a.width(), a.height()), pos, size, launcher)
        if rect is None:
            g = L._target_screen().availableGeometry()
            rect = beside_rect((g.x(), g.y(), g.width(), g.height()), launcher, size, gap=0)
        x, y, w, h = rect
        if (x, y, w, h) != (self.x(), self.y(), self.width(), self.height()):
            self.setGeometry(x, y, w, h)
        self._layout_grips()

    def _layout_grips(self) -> None:
        """四隅のつまみは常に、四辺のつまみは「全辺」の設定のときだけ出す。"""
        g, t = self._grip_size
        sides = self.launcher.lcfg["dnd_resize"] == "edges"
        for grip in self.grips:
            grip.setGeometry(*grip_rect(grip.edges, self.width(), self.height(), g, t))
            grip.setVisible(grip.corner or sides)
            grip.raise_()   # 本文のラベルより手前に

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_grips()
        if self.resizing:
            self.resize_done.start()

    def _on_resized(self) -> None:
        """つまみで変えた大きさ（倍率 1 に換算）や動かした位置を覚え、置き直す。"""
        if QApplication.mouseButtons() & Qt.LeftButton:   # まだドラッグ中
            self.resize_done.start()
            return
        L = self.launcher
        if self.geometry() == self.press_geo:   # 動かしていない
            self.resizing = self.moving = False
            return
        if self.resizing:
            s = L.lcfg["scale"]
            self.want = self.size()
            L.lcfg["dnd_size"] = [round(self.width() / s), round(self.height() / s)]
        moved = self.moving or (self.resizing and L.lcfg["dnd_pos"])
        if moved:
            L.lcfg["dnd_pos"] = [self.x(), self.y()]
        self.resizing = self.moving = False
        self.anchor()   # ランチャーに被っていたら一番近い辺へずれる
        if moved and L.lcfg["dnd_pos"]:
            L.lcfg["dnd_pos"] = [self.x(), self.y()]   # ずれた後の位置を覚える
        self.launcher.changed.emit()

    def dragEnterEvent(self, e):
        self.launcher.accept_drag(e)

    def dropEvent(self, e):
        self.launcher.drop_files(e)

    def contextMenuEvent(self, e):
        L = self.launcher
        m = QMenu(L.root)   # ランチャーと同じ見た目にする
        a = m.addAction(tr("menu.dnd_on_top"), lambda: L.set_on_top("dnd_on_top", not L.lcfg["dnd_on_top"]))
        a.setCheckable(True)
        a.setChecked(L.lcfg["dnd_on_top"])
        m.addAction(tr("menu.dnd_reset_size"), L.reset_panel_size)
        a = m.addAction(tr("menu.dnd_attach"), L.attach_panel)
        a.setEnabled(L.lcfg["dnd_pos"] is not None)
        m.addSeparator()
        m.addAction(tr("menu.dnd_exit"), L.toggle_mode)
        m.exec(e.globalPos())


class Flyout(QWidget):
    """同じカテゴリの残りのアプリを横に出す列（ランチャーに従属する最前面の小窓）。"""

    def __init__(self, parent: QWidget, tiles: list[AppTile], transparent: bool):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, parent.lcfg["on_top"])   # ランチャーの最前面設定に合わせる
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if transparent:
            self.setAttribute(Qt.WA_TranslucentBackground)
        self.root = QFrame(self, objectName="Root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        self.lay = QHBoxLayout(self.root)
        self.nav = QWidget()   # 段の中の左右入れ替え（Shift+ホイールと同じ）
        nav = QVBoxLayout(self.nav)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(0)
        for text, right in (("◀", False), ("▶", True)):
            b = QToolButton(text=text, toolTip=tr("flyout.swap_tip"))
            b.clicked.connect(lambda _=False, r=right: parent.flyout_step(self, r))
            nav.addWidget(b)
        self.lay.addWidget(self.nav)
        self.set_tiles(tiles)

    def set_tiles(self, tiles: list[AppTile]) -> None:
        self.tiles = tiles
        for t in tiles:
            self.lay.removeWidget(t)
            self.lay.addWidget(t)

    def wheelEvent(self, e):
        self.parent().wheelEvent(e)   # 別窓なのでランチャーへ届かない → 渡す

    def set_reverse(self, rev: bool) -> None:
        """左へ出すときは 1 個目を右端（元のタイル側）に置く。ボタンは元のタイルから遠い端。"""
        for i, t in enumerate([self.nav] + self.tiles[::-1] if rev else self.tiles + [self.nav]):
            self.lay.removeWidget(t)
            self.lay.insertWidget(i, t)

    def apply_scale(self, css: str, s: float) -> None:
        self.root.setStyleSheet(css)
        m = theme.px(6, s)
        self.lay.setContentsMargins(m, m, m, m)
        self.lay.setSpacing(m)
        self.adjustSize()


class LauncherWindow(QWidget):
    changed = Signal()                  # 設定が変わった → 保存してほしい
    settings_requested = Signal(object)
    quit_requested = Signal()

    def __init__(self, lcfg: dict, cfg: dict):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, lcfg["on_top"])
        self.lcfg, self.cfg = lcfg, cfg
        self.snap: dict = {}
        self.next_index = -1
        self.help_dialog = None
        self._in_taskbar = False
        self.setWindowTitle("DDLauncher")   # タスクバーに最小化したときの表示名
        transparent = True   # 透明度をリアルタイムに変えられるよう常に半透明対応で作る（濃さは theme.C の *_glass）
        if transparent:
            self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)

        self.root = QFrame(self, objectName="Root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)
        self.vbox = QVBoxLayout(self.root)
        # 畳んだとき = mini（アイコン 1 個）/ 展開したとき = body（ヘッダ・タイル・状態）
        self.expanded = None
        self.mini = MiniIcon(transparent)
        self.mini.entered.connect(lambda: self.set_expanded(True))
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.vbox.addWidget(self.mini)
        self.vbox.addWidget(self.body)
        v = self.body_lay

        # ヘッダ: 操作ボタンのみ（右寄せ）
        head = QHBoxLayout()
        head.setSpacing(2)
        if winproc.is_admin():   # ランチャーが昇格していると Explorer からの D&D が届かない
            head.addWidget(QLabel("⚠ADMIN", objectName="Warn", toolTip=tr("head.admin_warn_tip")))
        head.addStretch()
        self.mode_btn = self._btn("D&&D", tr("head.dnd_tip"), self.toggle_mode, checkable=True)
        self.seq_btn = self._btn("▶", tr("head.seq_tip"), self.toggle_sequence)
        self.seq_btn.setVisible(lcfg["show_seq_button"])
        for text, tip, step in (("▲", tr("head.prev_tip"), -1), ("▼", tr("head.next_tip"), 1)):
            head.addWidget(self._btn(text, tip, lambda _=False, st=step: self.rotate(st)))   # 輪を回す
        head.addWidget(self.mode_btn)
        head.addWidget(self.seq_btn)
        head.addWidget(self._btn("⚙", tr("head.settings_tip"), lambda: self.settings_requested.emit(self)))
        head.addWidget(self._btn("?", tr("head.help_tip"), self.show_help))
        v.addLayout(head)

        self.grid = QGridLayout()
        self.tiles = []
        for i, app in enumerate(lcfg["apps"]):
            t = AppTile(i, app, lcfg["scale"], transparent)
            t.launch_requested.connect(self.launch_one)
            t.hovered.connect(self.open_flyout)
            self.tiles.append(t)
        mini_i = next((i for i, a in enumerate(lcfg["apps"]) if a.get("path") and a["path"] == lcfg["mini_icon"]), None)
        if mini_i is not None:
            self.mini.set_app(lcfg["apps"][mini_i])
            self.tiles[mini_i].icon_ready.connect(self.mini.reload)   # サイトのアイコンはタイル側が取ってくる
        self.flyouts: dict[AppTile, Flyout] = {}
        self.heads: list[AppTile] = []   # 角から並ぶタイル（輪として回す）
        self.rot = 0
        for slot in linear_slots(lcfg["apps"]):
            head = self.tiles[slot[0]]
            self.heads.append(head)
            if len(slot) > 1:
                head.members = [self.tiles[i] for i in slot[1:]]
                self.flyouts[head] = Flyout(self, head.members, transparent)
        if lcfg["keep_rotation"] and self.heads:   # 保存した並びに戻す
            self.rot = lcfg["rot"] % len(self.heads)
            for h in list(self.flyouts):
                self.spin(h, lcfg["spins"].get(h.app.get("group"), 0) % (len(h.members) + 1), save=False)
        self._layout_tiles()
        v.addLayout(self.grid)
        self.fly_timer = QTimer(self, interval=100, timeout=self._check_flyout)
        self.fly_left_at = None
        self.hover_timer = QTimer(self, interval=100, timeout=self._check_hover)   # 展開中だけ動かす
        self.hover_left_at = None
        self.confirm_timer = QTimer(self, interval=1000, timeout=self._confirm_tick)
        self.confirm_deadline = 0.0

        # 状態表示は左寄せの見出しタブ
        status_row = QHBoxLayout()
        self.status = QLabel("", objectName="Status")
        status_row.addWidget(self.status)
        status_row.addStretch()
        v.addLayout(status_row)

        self.bridge = ObsBridge(cfg["obs"], self)
        self.bridge.finished.connect(lambda ok, msg: self.set_status(("OK " if ok else "NG ") + msg))
        self.bridge.names_ready.connect(self._resolve_names)
        self.seq = Sequencer(self)
        self.seq.progress.connect(self._on_progress)
        self.seq.done.connect(self._on_seq_done)
        self.anim = QTimer(self, timeout=self._anim_tick)

        self._hooked_screen = None
        self._fitting = False
        self.panel = DropPanel(self)
        self.apply_scale(lcfg["scale"])
        self._apply_mode()
        # 束縛メソッドで接続 → ウィンドウ破棄時に自動で切断される
        QGuiApplication.instance().screenAdded.connect(self._on_screens)
        QGuiApplication.instance().screenRemoved.connect(self._on_screens)
        self.place()

    def _layout_tiles(self) -> None:
        """角を配置コーナーに合わせて並べる（右下なら 横 = 左へ、縦 = 上へ伸びる）。"""
        if self.lcfg["layout"] == "group":   # GROUP: 角から縦一列（L 字の横 1 列ぶん）
            cells = group_cells(len(self.heads), self.lcfg["group_max_cols"], self.lcfg["group_max_rows"])
        else:
            cells = l_cells(len(self.heads), self.lcfg["max_cols"], self.lcfg["max_rows"])
        rows = max((r for r, _ in cells), default=0) + 1
        cols = max((c for _, c in cells), default=0) + 1
        bottom, right = "bottom" in self.lcfg["corner"], "right" in self.lcfg["corner"]
        for t, (r, c) in zip(self.heads[self.rot:] + self.heads[:self.rot], cells):
            self.grid.removeWidget(t)
            self.grid.addWidget(t, rows - 1 - r if bottom else r, cols - 1 - c if right else c)

    def _btn(self, text, tip, slot, checkable=False) -> QToolButton:
        b = QToolButton(text=text, toolTip=tip, checkable=checkable)
        b.clicked.connect(slot)
        return b

    # ---- 大きさ ----
    def apply_scale(self, s: float) -> None:
        s = round(min(max(s, SCALE_RANGE[0]), SCALE_RANGE[1]), 2)
        self.lcfg["scale"] = s
        # Root にだけ適用: ウィンドウに掛けると子の設定/ヘルプ画面まで水色・拡大になる
        css = theme.stylesheet(s)
        self.root.setStyleSheet(css)
        m = theme.px(6, s)
        self._apply_margins()
        self.vbox.setSpacing(0)
        self.body_lay.setSpacing(theme.px(5, s))
        self.grid.setSpacing(m)
        self.mini.set_scale(s)
        for t in self.tiles:
            t.set_scale(s)
        for f in self.flyouts.values():
            f.apply_scale(css, s)
        self.place()
        QTimer.singleShot(0, self.place)   # スタイルシートのフォント反映後にもう一度サイズ計算
        self._sync_panel()

    def refresh_colors(self) -> None:
        """theme.C を変えたあとに呼ぶ（カラーエディターのリアルタイム反映）。半透明 / 不透明の切り替えは作り直しが要る。"""
        for t in self.tiles + [self.mini]:
            t.bg = theme.qcolor(theme.C["tile_glass"])
            t.update()
        self.apply_scale(self.lcfg["scale"])   # スタイルシート（枠・文字・カテゴリ列・D&D 欄）を今の色で作り直す

    def _apply_margins(self) -> None:
        s = self.lcfg["scale"]
        m = theme.px(6, s)
        # 展開時はヘッダの上を詰める。畳んだアイコンは四辺同じ余白
        self.vbox.setContentsMargins(m, theme.px(4, s) if self.expanded else m, m, m)

    # ---- 畳む / 展開 ----
    def _force_expanded(self) -> bool:
        """D&D モード中（ドロップ先が要る）と起動シーケンス中（残り秒数を見せる）は畳まない。"""
        return self.lcfg["collapse"] == "always" or self.lcfg["mode"] == "dnd" or self.seq.running

    def set_expanded(self, on: bool) -> None:
        on = bool(on) or self._force_expanded()
        changed = on != self.expanded
        self.expanded = on
        if on and self.lcfg["collapse"] == "hover":
            if not self.hover_timer.isActive():
                self.hover_left_at = None
                self.hover_timer.start()
        else:
            self.hover_timer.stop()
        if not changed:
            return
        if not on:
            self._close_flyouts()
        self.body.setVisible(on)
        self.mini.setVisible(not on)
        self._apply_margins()
        self.place()
        QTimer.singleShot(0, self.place)   # レイアウト反映後にもう一度サイズ計算

    def _check_hover(self) -> None:
        if self._force_expanded() or QApplication.activePopupWidget() or self._cursor_near():
            self.hover_left_at = None
        elif self.hover_left_at is None:
            self.hover_left_at = time.monotonic()   # 外れた瞬間には畳まず、少し待つ
        elif time.monotonic() - self.hover_left_at >= FLYOUT_GRACE_SEC:
            self.set_expanded(False)

    def set_collapse(self, mode: str) -> None:
        self.lcfg["collapse"] = mode
        self.set_expanded(True)   # 切り替えた直後はマウスが乗っているので展開のまま
        self.changed.emit()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:   # Ctrl+ホイールで拡大縮小
            self.apply_scale(self.lcfg["scale"] + (0.1 if e.angleDelta().y() > 0 else -0.1))
            self.changed.emit()
            return
        if not self.lcfg["wheel_rotate"]:
            return
        d = e.angleDelta()
        dx = d.x() or (d.y() if e.modifiers() & Qt.ShiftModifier else 0)   # Shift+ホイール / チルトで段の中を横に回す
        if dx:
            w = QApplication.widgetAt(QCursor.pos())
            head = next((h for h in self.heads if w is h or w in h.members), None)
            if head:
                self.spin(head, -1 if dx > 0 else 1)
        elif self.heads and d.y():
            self.rotate(-1 if d.y() > 0 else 1)

    def flyout_step(self, fl, right: bool) -> None:
        """◀▶ ボタン: 見た目どおりの向きに段を 1 つずらす。"""
        head = next((h for h, f in self.flyouts.items() if f is fl), None)
        if head:
            self.spin(head, 1 if right == ("right" in self.lcfg["corner"]) else -1)

    def spin(self, head: AppTile, step: int, save: bool = True) -> None:
        """同じ段の切替: 段の中を回して、次（前）のアプリを列へ出す。"""
        fl = self.flyouts.pop(head, None)
        if fl is None:
            return
        stack = [head] + head.members
        stack = stack[step:] + stack[:step]
        new, head.members = stack[0], []
        new.members = stack[1:]
        self.heads[self.heads.index(head)] = new
        self.flyouts[new] = fl
        self.grid.removeWidget(head)
        fl.set_tiles(new.members)
        self._layout_tiles()
        if fl.isVisible():
            QTimer.singleShot(0, lambda: self.open_flyout(new))   # 列に入ったタイルの位置が確定してから合わせる（すぐだと上へ飛ぶ）
        if save and self.lcfg["keep_rotation"]:
            g, sp = new.app.get("group"), self.lcfg["spins"]
            sp[g] = (sp.get(g, 0) + step) % len(stack)
            self.changed.emit()

    def rotate(self, step: int) -> None:
        """輪を回して、次（前）のアプリを角へ送る。"""
        if len(self.heads) > 1:
            self.rot = (self.rot + step) % len(self.heads)
            self._close_flyouts()
            self._layout_tiles()
            if self.lcfg["keep_rotation"]:
                self.lcfg["rot"] = self.rot
                self.changed.emit()

    # ---- 配置 ----
    def _target_screen(self):
        screens = QGuiApplication.screens()
        idx = self.lcfg.get("screen", 0)
        return screens[idx] if 0 <= idx < len(screens) else QGuiApplication.primaryScreen()

    def _on_screens(self, *_):
        QTimer.singleShot(0, self.place)

    def place(self, *_) -> None:
        scr = self._target_screen()
        if scr is not self._hooked_screen:   # タスクバー表示切替・解像度変更で作業領域が変わったら再配置
            if self._hooked_screen is not None:
                try:
                    self._hooked_screen.availableGeometryChanged.disconnect(self.place)
                except (RuntimeError, TypeError):
                    pass
            scr.availableGeometryChanged.connect(self.place)
            self._hooked_screen = scr
        self.adjustSize()
        g = scr.availableGeometry()
        # 1 画面に必ず収める: はみ出すなら倍率を下げる（隣のモニタへ貫通させない）
        if (self.width() > g.width() or self.height() > g.height()) \
                and self.lcfg["scale"] > SCALE_RANGE[0] and not self._fitting:
            f = min(g.width() / self.width(), g.height() / self.height())
            self._fitting = True
            self.apply_scale(self.lcfg["scale"] * f * 0.97)   # apply_scale が place を呼び直す
            self._fitting = False
            return
        # D&D モード中は画面端にベタ付け（余白に落として別の窓へドロップする事故を防ぐ）
        flush = self.lcfg["mode"] == "dnd" and self.lcfg["dnd_flush"]
        x, y = corner_pos((g.x(), g.y(), g.width(), g.height()), (self.width(), self.height()),
                          self.lcfg["corner"], 0 if flush else self.lcfg["margin"])
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def set_corner(self, corner: str) -> None:
        self.lcfg["corner"] = corner
        self._layout_tiles()
        self.place()
        self.changed.emit()

    # ---- 状態 ----
    def update_status(self, snap: dict) -> None:
        self.snap = snap
        for t in self.tiles:
            t.set_state(*winproc.app_state(t.app, snap))
        self.place()   # 保険: 作業領域変化の通知を取りこぼしても 2 秒以内に戻る

    def set_obs(self, state) -> None:
        for t in self.tiles:
            t.set_obs(state)

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.adjustSize()
        self.place()

    # ---- 起動 ----
    def launch_one(self, index: int, admin, click: bool = False) -> None:
        app = self.lcfg["apps"][index]
        snap = winproc.snapshot()
        if click and winproc.obs_guarded(app, snap):
            self.set_status(tr("status.obs_guard", name=app["name"]))
            return
        running, _ = winproc.app_state(app, snap)
        if running and admin is None:
            self.set_status(tr("status.already_running", name=app["name"]))
            return
        ok, err = winproc.launch(app, admin)
        if ok:
            place_on_monitor(app, self)
        self.set_status(tr("status.launched", name=app["name"]) if ok else f"{app['name']} {err}")

    def toggle_sequence(self) -> None:
        if self.seq.running:
            self.seq.cancel()
        else:
            self.start_sequence()

    def start_sequence(self) -> None:
        self.seq.start(self.lcfg["apps"])

    def _on_progress(self, index: int, text: str) -> None:
        self.seq_btn.setText("■" if self.seq.running else "▶")
        if index != self.next_index:
            for t in self.tiles:
                t.highlight = 0.0
                t.update()
            self.next_index = index
            interval = ANIM_INTERVAL_MS[self.cfg["animation"]]
            if index >= 0 and interval:
                self.anim.start(interval)
            else:
                self.anim.stop()
                for t in self._hl_targets():   # Off: 静的ハイライト
                    t.highlight = 1.0
                    t.update()
        self.set_status(text)
        self.set_expanded(bool(self.expanded))   # シーケンス中は展開、終わったらマウスが離れたときに畳む

    def _hl_targets(self) -> list[AppTile]:
        """次に起動するタイル（重ねて隠れていれば表のタイルも）。"""
        if not 0 <= self.next_index < len(self.tiles):
            return []
        tile = self.tiles[self.next_index]
        return [tile] + [h for h in self.flyouts if tile in h.members]

    def _anim_tick(self) -> None:
        t = time.monotonic()
        if self.cfg["animation"] == "full":
            h = 0.5 + 0.5 * math.sin(t * 5)
        else:
            h = 1.0 if int(t * 2) % 2 else 0.2
        for tile in self._hl_targets():
            tile.highlight = h
            tile.update()

    # ---- シーケンス完了後: 全アプリの起動を確認してから最小化 / 隠す ----
    def _on_seq_done(self, errors: list) -> None:
        if errors or self.lcfg["after_sequence"] == "none":   # 失敗があれば見えるまま残す
            return
        self.confirm_deadline = time.monotonic() + CONFIRM_WAIT_SEC
        self.confirm_timer.start()

    def _confirm_tick(self) -> None:
        snap = winproc.snapshot()
        pending = [a["name"] for a in self.lcfg["apps"]
                   if a.get("autostart") and a.get("path") and not winproc.app_state(a, snap)[0]]
        if not pending:
            self.confirm_timer.stop()
            if self.lcfg["after_sequence"] == "minimize":
                self.minimize_to_taskbar()
            else:
                self.hide_to_tray()
        elif time.monotonic() > self.confirm_deadline:
            self.confirm_timer.stop()
            self.set_status(tr("status.not_started", names=", ".join(pending)))

    def _set_window_type(self, t) -> None:
        self.setWindowFlags((self.windowFlags() & ~Qt.WindowType_Mask) | t)

    def minimize_to_taskbar(self) -> None:
        # Tool 窓はタスクバーにボタンが出ないため、最小化中だけ通常窓にする（復帰時に Tool へ戻す）
        self._close_flyouts()
        self.panel.hide()
        self._in_taskbar = True
        self._set_window_type(Qt.Window)
        self.showMinimized()

    def hide_to_tray(self) -> None:
        self._close_flyouts()
        self.panel.hide()
        self.hide()   # 復帰はトレイアイコンから

    def restore(self) -> None:
        if self._in_taskbar:
            self._in_taskbar = False
            self._set_window_type(Qt.Tool)
        self.showNormal()
        self.raise_()
        self.place()
        self._sync_panel()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._sync_panel)

    def moveEvent(self, e):
        super().moveEvent(e)
        if getattr(self, "panel", None) and self.panel.isVisible():   # __init__ の途中（panel を作る前）にも呼ばれる
            self.panel.anchor()   # ランチャーが動いたり大きさが変わったら D&D スペースも付いて行く

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "panel", None) and self.panel.isVisible():
            self.panel.anchor()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.panel.hide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange and self._in_taskbar and not self.isMinimized():
            QTimer.singleShot(0, self.restore)   # タスクバーから戻されたら Tool 窓に戻す

    # ---- 同じカテゴリの横に出るアプリ列 ----
    def open_flyout(self, gt: AppTile) -> None:
        if gt not in self.heads:   # 横に出ている側のタイル
            return
        fl = self.flyouts.get(gt)
        for other in self.flyouts.values():
            if other is not fl:
                other.hide()
        if fl is None:   # LINEAR で重ねていないタイル
            return
        to_left = "right" in self.lcfg["corner"]   # 画面の内側へ出す
        fl.set_reverse(to_left)
        fl.adjustSize()
        g = self._target_screen().availableGeometry()
        s = self.lcfg["scale"]
        gap = theme.px(4, s)
        tx = gt.mapToGlobal(QPoint(0, 0)).x()   # タイルのすぐ横
        x = tx - fl.width() - gap if to_left else tx + gt.width() + gap
        y = gt.mapToGlobal(QPoint(0, 0)).y() + gt.height() // 2 - fl.height() // 2
        x = max(g.left(), min(x, g.right() + 1 - fl.width()))
        y = max(g.top(), min(y, g.bottom() + 1 - fl.height()))
        if not fl.isVisible() and self.cfg["animation"] == "full":
            fl.move(x + theme.px(24, s) * (1 if to_left else -1), y)   # ランチャー側から滑り出す
            fl.show()
            a = QPropertyAnimation(fl, b"pos", fl)
            a.setDuration(120)
            a.setEndValue(QPoint(x, y))
            a.start(QAbstractAnimation.DeleteWhenStopped)
        else:   # Reduced / Off は即表示
            fl.move(x, y)
            fl.show()
        self.fly_timer.start()

    def _check_flyout(self) -> None:
        if QApplication.activePopupWidget():   # タイルの右クリックメニュー表示中は閉じない
            return
        if self._cursor_near():
            self.fly_left_at = None
        elif self.fly_left_at is None:
            self.fly_left_at = time.monotonic()   # 外れた瞬間には閉じず、少し待つ
        elif time.monotonic() - self.fly_left_at >= FLYOUT_GRACE_SEC:
            self._close_flyouts()

    def _cursor_near(self) -> bool:
        """マウスがランチャーか、横に出た列の上にあるか。"""
        pos = QCursor.pos()
        pad = theme.px(10, self.lcfg["scale"])   # ランチャーと列の隙間を横切る間に閉じないよう余裕を持たせる
        areas = [self.frameGeometry()] + [f.frameGeometry() for f in self.flyouts.values() if f.isVisible()]
        return any(r.adjusted(-pad, -pad, pad, pad).contains(pos) for r in areas)

    def _close_flyouts(self) -> None:
        for f in self.flyouts.values():
            f.hide()
        self.fly_timer.stop()
        self.fly_left_at = None

    # ---- D&D モード ----
    def toggle_mode(self) -> None:
        self.lcfg["mode"] = "normal" if self.lcfg["mode"] == "dnd" else "dnd"
        self._apply_mode()
        self.changed.emit()

    def _apply_mode(self) -> None:
        dnd = self.lcfg["mode"] == "dnd"
        self.mode_btn.setChecked(dnd)
        self.root.setProperty("dnd", dnd)
        self.root.style().unpolish(self.root)
        self.root.style().polish(self.root)
        scene = self.cfg["obs"]["target_scene"] or tr("scene.current")
        self.set_status(f"DROP ▸ {scene}" if dnd else "")
        self._sync_panel()
        self.set_expanded(bool(self.expanded))

    def _sync_panel(self) -> None:
        """D&D モード中でランチャーが見えているあいだだけ、画面中央に説明欄を出す。"""
        if self.lcfg["mode"] == "dnd" and self.isVisible() and not self.isMinimized():
            self.panel.refresh(self.lcfg["scale"])
            self.panel.show()
        else:
            self.panel.hide()

    def _exts(self) -> dict | None:
        """読み込む拡張子。「種類を選ぶ」がオフなら None（既定の拡張子）。"""
        return self.lcfg["dnd_exts"] if self.lcfg["dnd_pick"] else None

    def _drop_paths(self, e) -> list[str]:
        """受け付けるもの。「種類を選ぶ」がオンならフォルダも（中身はドロップの後で見る）。"""
        if self.lcfg["mode"] != "dnd" or not e.mimeData().hasUrls():
            return []
        pick, exts = self.lcfg["dnd_pick"], self._exts()
        local = (u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile())
        return [p for p in local if (pick and os.path.isdir(p)) or source_spec(p, exts)]

    def accept_drag(self, e) -> None:
        if self._drop_paths(e):
            e.acceptProposedAction()

    def dragEnterEvent(self, e):
        if self.lcfg["dnd_on_launcher"]:   # オフならランチャーの上には落とせない（D&D スペースだけ）
            self.accept_drag(e)

    def dropEvent(self, e):
        if self.lcfg["dnd_on_launcher"]:
            self.drop_files(e)

    def drop_files(self, e) -> None:
        paths = self._drop_paths(e)
        if paths:
            e.acceptProposedAction()
            # 確認画面やフォルダの中を見る処理は、ドロップを受け取ってから（ドロップ中に止めるとエクスプローラー側が固まる）
            QTimer.singleShot(0, lambda: self._drop_picked(paths) if self.lcfg["dnd_pick"] else self._check_count(paths))

    def _drop_picked(self, paths: list[str]) -> None:
        """「種類を選ぶ」がオン: フォルダの中も集め、種類が混ざっていたら（またはフォルダなら）種類を選んでもらう。"""
        exts = self._exts()
        files, truncated = collect_files(paths, exts)
        if not files:
            self.set_status(tr("dnd.pick.none"))
        elif truncated or len({file_kind(f, exts) for f in files}) > 1 or any(os.path.isdir(p) for p in paths):
            self._pick_kinds(files, truncated)
        else:
            self._check_count(files)

    def _check_count(self, paths: list[str]) -> None:
        limit = self.lcfg["dnd_warn_count"]
        if limit and len(paths) >= limit:
            self._confirm_many(paths)
        else:
            self._send_files(paths)

    def _pick_kinds(self, files: list[str], truncated: bool) -> None:
        """読み込む種類（画像 / 映像 / 音楽）を選ぶ画面。数も見せるので、件数の確認も兼ねる。既定のボタンはキャンセル。"""
        exts = self._exts()
        groups = {k: [f for f in files if file_kind(f, exts) == k] for k in config.DND_KINDS}
        d = QDialog(self)
        d.setWindowTitle(tr("dnd.pick.title"))
        v = QVBoxLayout(d)
        v.addWidget(QLabel(tr("dnd.pick.body"), wordWrap=True))
        checks = {k: QCheckBox(f"{tr('kind.' + k)}  ({len(fs)})", checked=True) for k, fs in groups.items() if fs}
        for cb in checks.values():
            v.addWidget(cb)
        if truncated:
            v.addWidget(QLabel(tr("dnd.pick.truncated", n=len(files)), wordWrap=True))
        total = QLabel()
        v.addWidget(total)
        bb = QDialogButtonBox()
        add = bb.addButton(tr("dnd.pick.add"), QDialogButtonBox.AcceptRole)
        cancel = bb.addButton(tr("btn.cancel"), QDialogButtonBox.RejectRole)
        add.setAutoDefault(False)
        cancel.setDefault(True)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)

        def update():
            n = sum(len(groups[k]) for k, cb in checks.items() if cb.isChecked())
            total.setText(tr("dnd.pick.total", n=n))
            add.setEnabled(n > 0)

        for cb in checks.values():
            cb.toggled.connect(update)
        update()
        if d.exec() == QDialog.Accepted:
            self._send_files([f for f in files if checks[file_kind(f, exts)].isChecked()])
        else:
            self.set_status(tr("dnd.many.cancelled"))

    def _send_files(self, paths: list[str]) -> None:
        self.set_status(tr("status.sending", n=len(paths)))
        if self.cfg["obs"]["add"]["on_conflict"] == "ask":
            self.bridge.check_names(paths, self._exts())   # 先に OBS の名前を見て、被ったら _resolve_names で聞く
        else:
            self.bridge.add_files(paths, self._exts())

    def _resolve_names(self, data) -> None:
        """名前が被ったファイルがあれば、どうするかを選んでもらってから追加する。"""
        from conflict_dialog import ConflictDialog
        paths, exts, inputs, taken = data
        opt = self.cfg["obs"]["add"]
        conflicts = name_conflicts(paths, opt, taken)
        if not conflicts:
            self.bridge.add_files(paths, exts)
            return
        clash = {c[0] for c in conflicts}
        fixed = {base_name(p, opt) for p in paths if p not in clash}
        d = ConflictDialog(conflicts, fixed, inputs, taken, self)
        if d.exec() == QDialog.Accepted:
            self.bridge.add_files(paths, exts, d.plan())
        else:
            self.set_status(tr("dnd.many.cancelled"))

    def _confirm_many(self, paths: list[str]) -> None:
        """まとめてドロップしたときの確認（大量のソースを誤って OBS に入れる事故を防ぐ）。既定は「いいえ」。"""
        shown = 10
        names = "\n".join("・" + os.path.basename(p) for p in paths[:shown])   # 配信に映ってもフォルダ構造が見えないよう名前だけ
        if len(paths) > shown:
            names += "\n" + tr("dnd.many.more", n=len(paths) - shown)
        box = QMessageBox(QMessageBox.Warning, tr("dnd.many.title"), tr("dnd.many.body", n=len(paths)) + "\n\n" + names,
                          QMessageBox.Yes | QMessageBox.No, self)
        box.button(QMessageBox.Yes).setText(tr("btn.yes"))
        box.button(QMessageBox.No).setText(tr("btn.no"))
        box.setDefaultButton(QMessageBox.No)
        if box.exec() == QMessageBox.Yes:
            self._send_files(paths)
        else:
            self.set_status(tr("dnd.many.cancelled"))

    # ---- メニュー / ヘルプ ----
    def contextMenuEvent(self, e):
        m = QMenu(self.root)   # Root のスタイルを継承させる
        m.addAction(tr("menu.seq_stop" if self.seq.running else "menu.seq_start"), self.toggle_sequence)
        a = m.addAction(tr("menu.dnd"), self.toggle_mode)
        a.setCheckable(True)
        a.setChecked(self.lcfg["mode"] == "dnd")
        pos = m.addMenu(tr("menu.corner"))
        for key in ("top_left", "top_right", "bottom_left", "bottom_right"):
            act = pos.addAction(tr(f"corner.{key}"), lambda k=key: self.set_corner(k))
            act.setCheckable(True)
            act.setChecked(self.lcfg["corner"] == key)
        always = self.lcfg["collapse"] == "always"
        a = m.addAction(tr("menu.always_expand"), lambda: self.set_collapse("hover" if always else "always"))
        a.setCheckable(True)
        a.setChecked(always)
        for key, label in (("on_top", "menu.on_top"), ("dnd_on_top", "menu.dnd_on_top")):
            a = m.addAction(tr(label), lambda k=key: self.set_on_top(k, not self.lcfg[k]))
            a.setCheckable(True)
            a.setChecked(self.lcfg[key])
        m.addAction(tr("menu.settings"), lambda: self.settings_requested.emit(self))
        m.addAction(tr("menu.help"), self.show_help)
        m.addSeparator()
        m.addAction(tr("menu.quit"), self.quit_requested.emit)
        m.exec(e.globalPos())

    def set_on_top(self, key: str, on: bool) -> None:
        """key: on_top（ランチャー）/ dnd_on_top（D&D スペース）。フラグを変えると Qt が窓を隠すので出し直す。"""
        self.lcfg[key] = on
        w = self if key == "on_top" else self.panel
        visible = w.isVisible()
        w.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if visible:
            w.show()
        if key == "on_top":
            for f in self.flyouts.values():   # 横に出る列もランチャーに合わせる（閉じている間に変えれば出し直し不要）
                f.hide()
                f.setWindowFlag(Qt.WindowStaysOnTopHint, on)
            if visible:
                self.place()
                self._sync_panel()
        self.changed.emit()

    def attach_panel(self) -> None:
        self.lcfg["dnd_pos"] = None
        self._sync_panel()
        self.changed.emit()

    def reset_panel_size(self) -> None:
        self.lcfg["dnd_size"] = [0, 0]
        self._sync_panel()
        self.changed.emit()

    def reset_panel(self) -> None:
        """D&D スペースを既定の大きさでランチャーの隣に戻す（画面の外などへ行ってしまったとき用）。"""
        self.lcfg["dnd_size"] = [0, 0]
        self.lcfg["dnd_pos"] = None
        self._sync_panel()
        self.changed.emit()

    def show_help(self) -> None:
        if self.help_dialog is None:
            d = QDialog(self)
            d.setWindowTitle("DDLauncher Help")
            d.resize(QSize(560, 600))
            tb = QTextBrowser(d)
            tb.setOpenExternalLinks(True)
            tb.setSearchPaths([os.path.join(i18n.LOCALE_DIR, "help")])   # 挿絵 img/*.png を help フォルダから読む
            tb.setHtml(i18n.help_html())
            QVBoxLayout(d).addWidget(tb)
            self.help_dialog = d
        self.show_beside(self.help_dialog)

    def show_beside(self, dlg: QWidget) -> None:
        """パネルをランチャーに被らない位置に表示する（ランチャーは最前面なので被るとボタンが押せない）。"""
        dlg.show()
        g = self._target_screen().availableGeometry()
        fg, cg = dlg.frameGeometry(), dlg.geometry()
        frame_w, frame_h = fg.width() - cg.width(), fg.height() - cg.height()   # タイトルバー等
        x, y, w, h = beside_rect((g.x(), g.y(), g.width(), g.height()),
                                 (self.x(), self.y(), self.width(), self.height()), (fg.width(), fg.height()))
        dlg.resize(w - frame_w, h - frame_h)
        dlg.move(x, y)
        dlg.raise_()
        dlg.activateWindow()

    def shutdown(self) -> None:
        if self.seq.running:
            self.seq.cancel()
        self.anim.stop()
        self.confirm_timer.stop()
        self.hover_timer.stop()
        self._close_flyouts()
        self.panel.close()
        self.panel.deleteLater()   # 親を持たない窓なので、ランチャーと一緒には消えない
        self.close()
        self.deleteLater()
