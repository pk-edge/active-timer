"""
FloatingWidget — Edge brand widget.

Solid #4a0f70 tile, no glass/transparency.
Thin 1.5 px animated "running light" border whose colour changes with
sync status. The bright head sweeps clockwise; a fading tail follows.

Running-light colours
─────────────────────
  Synced    → bright green   #22c55e
  Syncing   → bright blue    #3b82f6
  Idle      → bright amber   #f59e0b
  Offline   → bright red     #ef4444
  Enrolling → bright cyan    #06b6d4
"""

import json
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QLabel, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QSystemTrayIcon, QMenu, QApplication, QMessageBox,
)
from PySide6.QtCore  import Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve
from PySide6.QtGui   import (
    QFont, QColor, QPainter, QPainterPath, QPen,
    QConicalGradient, QLinearGradient, QIcon, QPixmap, QBrush, QCursor,
)

# ── Geometry ──────────────────────────────────────────────────────────────────
PAD    = 6          # transparent padding — gives border room to glow
W_CARD = 228
H_CARD = 84
W = W_CARD + PAD * 2   # 240
H = H_CARD + PAD * 2   # 96
RADIUS = 13

# ── Brand colour — slightly transparent so desktop shows through ──────────────
BRAND   = QColor(0x4a, 0x0f, 0x70, 205)   # #4a0f70 at ~80 % opacity

# ── Running-light status colours ──────────────────────────────────────────────
LIGHT = {
    "active":    QColor(0x22, 0xc5, 0x5e),   # green
    "syncing":   QColor(0x3b, 0x82, 0xf6),   # blue
    "idle":      QColor(0xf5, 0x9e, 0x0b),   # amber
    "offline":   QColor(0xef, 0x44, 0x44),   # red
    "enrolling": QColor(0x06, 0xb6, 0xd4),   # cyan
}
# Dim ambient border (very subtle glow of the brand colour)
DIM_BORDER = QColor(0x7a, 0x3f, 0xa0, 90)

# ── Status-dot text colour ────────────────────────────────────────────────────
DOT_TXT = {
    "ok":        "#4ade80",
    "syncing":   "#60a5fa",
    "offline":   "#f87171",
    "enrolling": "#34d399",
    "error":     "#f87171",
}
STATUS_LABELS = {
    "ok":        "synced",
    "syncing":   "syncing…",
    "offline":   "offline",
    "enrolling": "connecting…",
    "error":     "error",
}

# ── Text colours on the dark purple background ────────────────────────────────
C_CLOCK = "#ffffff"
C_SUB   = "rgba(233, 213, 255, 210)"   # lavender
C_INFO  = "rgba(216, 196, 235, 160)"   # muted lavender

# ── Fonts ─────────────────────────────────────────────────────────────────────
if sys.platform == "darwin":
    FONT_CLOCK = QFont("Menlo",          28, QFont.Bold)
    FONT_SUB   = QFont("Helvetica Neue", 10)
    FONT_INFO  = QFont("Helvetica Neue", 10)
else:
    FONT_CLOCK = QFont("Consolas",       23, QFont.Bold)
    FONT_SUB   = QFont("Segoe UI",       10)
    FONT_INFO  = QFont("Segoe UI",       10)


def _glow(w: QLabel, color: QColor, radius: int) -> None:
    fx = QGraphicsDropShadowEffect(w)
    fx.setBlurRadius(radius)
    fx.setColor(color)
    fx.setOffset(0, 0)
    w.setGraphicsEffect(fx)


def _tray_icon(c: QColor) -> QIcon:
    px = QPixmap(16, 16)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 12, 12)
    p.end()
    return QIcon(px)


def _date_str() -> str:
    d = datetime.now()
    return d.strftime("%b ") + str(d.day)


def _pos_file() -> Path:
    from config import Config
    return Config.BASE_DIR / "widget_pos.json"


def _load_pos():
    try:
        d = json.loads(_pos_file().read_text())
        return QPoint(int(d["x"]), int(d["y"]))
    except Exception:
        return None


def _save_pos(pt: QPoint) -> None:
    try:
        _pos_file().write_text(json.dumps({"x": pt.x(), "y": pt.y()}))
    except Exception:
        pass


class FloatingWidget(QWidget):
    def __init__(self, shared: dict):
        super().__init__()
        self._shared      = shared
        self._elapsed     = 0
        self._drag_pos    = QPoint()
        self._last_status = ""
        self._last_app    = ""
        self._is_idle     = False
        self._light_key   = "syncing"   # drives the running-light colour
        self._border_deg  = 0.0         # current head angle (degrees)

        self._loaded_today = False
        self._session_date = datetime.now().date()
        self._last_tick    = int(time.time())

        self._setup_window()
        self._build_ui()
        self._setup_tray()
        self._start_timers()

    @property
    def elapsed(self) -> int:
        return self._elapsed

    # ── Window ────────────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Window
        )
        # Translucent so rounded corners are clipped cleanly
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)

        saved = _load_pos()
        if saved:
            self.move(saved)
        else:
            screen = QApplication.primaryScreen()
            if screen:
                g = screen.availableGeometry()
                self.move(g.right() - W - 20, g.top() + 20)

        QTimer.singleShot(300, self._apply_macos_all_spaces)

    def _apply_macos_all_spaces(self):
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApp
            NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
            for win in NSApp.windows():
                win.setCollectionBehavior_(
                    win.collectionBehavior() | NSWindowCollectionBehaviorCanJoinAllSpaces
                )
        except Exception:
            pass

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        card = QPainterPath()
        card.addRoundedRect(PAD, PAD, W_CARD, H_CARD, RADIUS, RADIUS)

        # ── Solid brand-colour fill ───────────────────────────────────────────
        p.setClipPath(card)
        p.fillPath(card, QBrush(BRAND))
        p.setClipping(False)

        # ── Dim ambient border (always visible, very faint) ───────────────────
        p.setPen(QPen(DIM_BORDER, 1.0))
        p.drawPath(card)

        # ── Running-light border (rotating conical gradient) ──────────────────
        light_c = LIGHT.get(self._light_key, LIGHT["syncing"])
        lr, lg, lb = light_c.red(), light_c.green(), light_c.blue()

        # QConicalGradient: angle is where colour-at-0 starts, sweeps CCW.
        # We advance _border_deg clockwise so we negate it.
        cx = PAD + W_CARD / 2.0
        cy = PAD + H_CARD / 2.0
        cg = QConicalGradient(cx, cy, -self._border_deg)

        # Head: bright, sharp (covers ~8% of circumference)
        # Tail: fades over ~20%
        # Rest: dim ambient
        cg.setColorAt(0.000, QColor(lr, lg, lb, 255))
        cg.setColorAt(0.040, QColor(lr, lg, lb, 200))
        cg.setColorAt(0.080, QColor(lr, lg, lb, 80))
        cg.setColorAt(0.160, QColor(lr, lg, lb, 20))
        cg.setColorAt(0.170, QColor(lr, lg, lb, 10))
        cg.setColorAt(0.999, QColor(lr, lg, lb, 10))
        cg.setColorAt(1.000, QColor(lr, lg, lb, 255))  # seamless wrap

        p.setPen(QPen(QBrush(cg), 1.8))
        p.drawPath(card)

    # ── UI labels ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        px   = PAD
        yclk = px + 4
        ysub = px + 42
        yinf = px + H_CARD - 21

        # Clock
        self._clock = QLabel("00:00:00", self)
        self._clock.setFont(FONT_CLOCK)
        self._clock.setStyleSheet(f"color:{C_CLOCK}; background:transparent;")
        self._clock.setGeometry(px, yclk, W_CARD, 38)
        self._clock.setAlignment(Qt.AlignCenter)
        _glow(self._clock, QColor(255, 255, 255, 200), 20)

        # Sub-label
        self._sub_lbl = QLabel("Active Today", self)
        self._sub_lbl.setFont(FONT_SUB)
        self._sub_lbl.setStyleSheet(f"color:{C_SUB}; background:transparent;")
        self._sub_lbl.setGeometry(px, ysub, W_CARD, 18)
        self._sub_lbl.setAlignment(Qt.AlignCenter)

        # ── Info strip layout ─────────────────────────────────────────────────
        # Date   |   · synced (centred)   |   AppName (right, elided)
        #  40px         100px centre            rest, right-aligned

        # Date — left edge
        self._date_lbl = QLabel("", self)
        self._date_lbl.setFont(FONT_INFO)
        self._date_lbl.setStyleSheet(f"color:{C_INFO}; background:transparent;")
        self._date_lbl.setGeometry(px + 8, yinf, 42, 17)
        self._date_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # Status section centred: dot (12px) + text (76px) = 88px block,
        # centred inside W_CARD → starts at (W_CARD/2 - 44) from PAD
        cx = px + W_CARD // 2          # centre x of card
        dot_w   = 13
        txt_w   = 78
        block_w = dot_w + txt_w        # 91 px total
        dot_x   = cx - block_w // 2
        txt_x   = dot_x + dot_w

        # Dot (pulsing) — true centre of card
        self._dot_lbl = QLabel("●", self)
        self._dot_lbl.setFont(FONT_INFO)
        self._dot_lbl.setStyleSheet(f"color:{DOT_TXT['ok']}; background:transparent;")
        self._dot_lbl.setGeometry(dot_x, yinf, dot_w, 17)
        self._dot_lbl.setAlignment(Qt.AlignCenter)

        self._dot_opacity = QGraphicsOpacityEffect(self._dot_lbl)
        self._dot_opacity.setOpacity(1.0)
        self._dot_lbl.setGraphicsEffect(self._dot_opacity)

        self._dot_anim = QPropertyAnimation(self._dot_opacity, b"opacity", self)
        self._dot_anim.setDuration(1100)
        self._dot_anim.setStartValue(1.0)
        self._dot_anim.setEndValue(0.15)
        self._dot_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._dot_anim.setLoopCount(-1)
        self._dot_anim.start()

        # Status text (static, immediately right of dot)
        self._status_lbl = QLabel("synced", self)
        self._status_lbl.setFont(FONT_INFO)
        self._status_lbl.setStyleSheet(f"color:{DOT_TXT['ok']}; background:transparent;")
        self._status_lbl.setGeometry(txt_x, yinf, txt_w, 17)
        self._status_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # App name — right side, elide if too long
        app_x = px + W_CARD - 10 - 55  # 55 px wide, flush right
        self._app_lbl = QLabel("", self)
        self._app_lbl.setFont(FONT_INFO)
        self._app_lbl.setStyleSheet(f"color:{C_INFO}; background:transparent;")
        self._app_lbl.setGeometry(app_x, yinf, 55, 17)
        self._app_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        try:
            self._tray = QSystemTrayIcon(_tray_icon(BRAND), self)
            m = QMenu()
            m.addAction("Show timer",          self._show_raise)
            m.addSeparator()
            m.addAction("Restart Computer",    self._restart_computer)
            m.addAction("Shut Down Computer",  self._shutdown_computer)
            m.addSeparator()
            m.addAction("Quit TimeTracker (admin)", self._quit_requires_admin)
            self._tray.setContextMenu(m)
            self._tray.activated.connect(self._tray_clicked)
            self._tray.setToolTip("TimeTracker")
            self._tray.show()
        except Exception:
            pass

    def _tray_clicked(self, r):
        if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_raise()

    def _show_raise(self):
        self.show(); self.raise_(); self.activateWindow()

    def _quit_requires_admin(self):
        if sys.platform == "win32":
            from protection.win_admin import confirm_admin_elevation
            if not confirm_admin_elevation("Quit TimeTracker",
                    "Administrator approval is required to stop TimeTracker."):
                QMessageBox.warning(self, "TimeTracker",
                    "Administrator credentials required to quit TimeTracker.")
                return
        QApplication.quit()

    def _shutdown_computer(self):
        if QMessageBox.question(None, "Shut Down", "Shut down this computer?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._shared["db"].end_session(self._shared.get("current_session_id"), self._elapsed)
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to shut down'], check=False)
        else:
            subprocess.run(["shutdown", "-h", "+0"], check=False)

    def _restart_computer(self):
        if QMessageBox.question(None, "Restart", "Restart this computer?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._shared["db"].end_session(self._shared.get("current_session_id"), self._elapsed)
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to restart'], check=False)
        else:
            subprocess.run(["shutdown", "-r", "+0"], check=False)

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        self._t1 = QTimer(self); self._t1.timeout.connect(self._tick_clock);  self._t1.start(1000)
        self._t2 = QTimer(self); self._t2.timeout.connect(self._tick_status); self._t2.start(3000)
        # Border animation — 40 ms ≈ 25 fps, 3°/frame = ~120°/s ≈ 3 s/rotation
        self._t3 = QTimer(self); self._t3.timeout.connect(self._advance_border); self._t3.start(40)

    def _advance_border(self):
        self._border_deg = (self._border_deg + 3.0) % 360.0
        self.update()

    # ── Tick: clock ───────────────────────────────────────────────────────────

    def _tick_clock(self):
        now   = int(time.time())
        today = datetime.now().date()

        if not self._loaded_today:
            ts = self._shared.get("today_active_secs")
            if ts is not None:
                self._elapsed = ts; self._session_date = today
                self._loaded_today = True; self._last_tick = now
                self._redraw()

        if self._session_date and today > self._session_date:
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            nid = db.start_session(); self._shared["current_session_id"] = nid
            self._elapsed = 0; self._session_date = today; self._last_tick = now
            self._redraw()
            s = self._shared.get("new_session_event")
            if s: s.clear()
            return

        s = self._shared.get("new_session_event")
        if s and s.is_set():
            s.clear()
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            nid = db.start_session(); self._shared["current_session_id"] = nid
            self._elapsed = 0; self._session_date = today; self._last_tick = now
            self._redraw()

        pause = self._shared.get("pause_event")
        if pause and pause.is_set():
            self._last_tick = now; return

        gap = now - self._last_tick; self._last_tick = now
        if gap > 10: return

        self._elapsed += 1
        self._redraw()

    def _redraw(self):
        h, r = divmod(self._elapsed, 3600)
        m, s = divmod(r, 60)
        self._clock.setText(f"{h:02d}:{m:02d}:{s:02d}")

    # ── Tick: status / date / app ─────────────────────────────────────────────

    def _tick_status(self):
        self._date_lbl.setText(_date_str())

        pause   = self._shared.get("pause_event")
        is_idle = bool(pause and pause.is_set())
        if is_idle != self._is_idle:
            self._is_idle = is_idle
            self._light_key = "idle" if is_idle else self._status_to_key(
                self._shared.get("sync_status", "syncing"))
        if is_idle:
            self._sub_lbl.setText("Idle")
            self._sub_lbl.setStyleSheet("color:rgba(251,191,36,210); background:transparent;")
        else:
            self._sub_lbl.setText("Active Today")
            self._sub_lbl.setStyleSheet(f"color:{C_SUB}; background:transparent;")

        status = self._shared.get("sync_status", "syncing")
        if status != self._last_status:
            self._last_status = status
            color = DOT_TXT.get(status, DOT_TXT["syncing"])
            label = STATUS_LABELS.get(status, status)
            self._dot_lbl.setStyleSheet(f"color:{color}; background:transparent;")
            self._status_lbl.setText(label)
            self._status_lbl.setStyleSheet(f"color:{color}; background:transparent;")
            if not is_idle:
                self._light_key = self._status_to_key(status)
            self._tray.setIcon(_tray_icon(LIGHT.get(self._light_key, LIGHT["syncing"])))

        app = self._shared.get("current_app", "")
        # Elide using font metrics so it always fits the 55px label width
        fm = self._app_lbl.fontMetrics()
        app = fm.elidedText(app, Qt.ElideRight, 53)
        if app != self._last_app:
            self._last_app = app
            self._app_lbl.setText(app)

    @staticmethod
    def _status_to_key(s: str) -> str:
        if s in ("offline", "error"):   return "offline"
        if s == "syncing":              return "syncing"
        if s == "enrolling":            return "enrolling"
        return "active"

    # ── Drag + persist ────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.LeftButton and not self._drag_pos.isNull():
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and not self._drag_pos.isNull():
            _save_pos(self.pos())
        self._drag_pos = QPoint()

    def closeEvent(self, e):
        e.ignore(); self.show()
