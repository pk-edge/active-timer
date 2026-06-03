"""
FloatingWidget — PySide6 always-on-top timer overlay.

Uses an opaque window painted with rounded corners via paintEvent —
avoids WA_TranslucentBackground which causes macOS Core Animation to
composite the window at 60 fps, burning 40-50% CPU for a simple overlay.

Layout (280 x 80):
  Row 1:  dot  HH:MM:SS
  ──────────────────────
  Row 2:  ● sync status            active app
"""

import sys
import time
import subprocess
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QLabel,
    QSystemTrayIcon, QMenu, QApplication, QMessageBox,
)
from PySide6.QtCore  import Qt, QTimer, QPoint
from PySide6.QtGui   import (
    QFont, QColor, QPainter, QPainterPath,
    QIcon, QPixmap, QBrush, QCursor,
)

W, H   = 280, 80
RADIUS = 14
BG     = QColor(12, 12, 18, 230)
BORDER = QColor(255, 255, 255, 30)

STATUS_COLORS = {"ok": "#22c55e", "syncing": "#f59e0b", "offline": "#ef4444", "enrolling": "#f59e0b", "error": "#ef4444"}
STATUS_LABELS = {
    "ok":        "● synced",
    "syncing":   "● syncing…",
    "offline":   "● offline",
    "enrolling": "● connecting…",
    "error":     "● invalid token",
}

if sys.platform == "darwin":
    FONT_CLOCK = QFont("Menlo",          20, QFont.Bold)
    FONT_SMALL = QFont("Helvetica Neue",  9)
    FONT_APP   = QFont("Helvetica Neue", 10)
else:
    FONT_CLOCK = QFont("Courier New",    18, QFont.Bold)
    FONT_SMALL = QFont("Segoe UI",        8)
    FONT_APP   = QFont("Segoe UI",        9)


def _dot_px(color: str, size: int = 10) -> QPixmap:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return px


_DOT_CACHE: dict[str, QPixmap] = {}

def _dot(color: str) -> QPixmap:
    if color not in _DOT_CACHE:
        _DOT_CACHE[color] = _dot_px(color)
    return _DOT_CACHE[color]


class FloatingWidget(QWidget):
    def __init__(self, shared: dict):
        super().__init__()
        self._shared      = shared
        self._elapsed     = 0       # active seconds for today (loaded after enrollment)
        self._drag_pos    = QPoint()
        self._last_status = ""
        self._last_app    = ""
        self._last_screen = None    # for multi-monitor tracking

        # Daily resume / sleep-wake tracking
        self._loaded_today  = False                 # True once we've loaded today's accumulated secs
        self._session_date  = datetime.now().date() # date the current session belongs to
        self._last_tick     = int(time.time())      # wall-clock time of last tick (sleep-gap detection)

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
        # Opaque window — no transparency compositing cost
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self._move_to_screen(QApplication.primaryScreen())
        # Make widget appear on all Mission Control spaces (deferred until window handle exists)
        QTimer.singleShot(300, self._apply_macos_all_spaces)

    def _apply_macos_all_spaces(self):
        """Set NSWindow collection behavior so the widget shows on every space."""
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

    def _move_to_screen(self, screen):
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - W - 20, geo.top() + 20)
        self._last_screen = screen

    # ── Paint rounded background ──────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, W, H, RADIUS, RADIUS)
        p.setClipPath(path)
        p.fillPath(path, QBrush(BG))
        p.setPen(BORDER)
        p.drawPath(path)
        # Divider line
        p.setPen(QColor(255, 255, 255, 20))
        p.drawLine(14, 45, W - 14, 45)

    # ── UI labels ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Row 1 — status dot
        self._dot_lbl = QLabel(self)
        self._dot_lbl.setPixmap(_dot("#22c55e"))
        self._dot_lbl.setGeometry(14, 17, 10, 10)

        # Row 1 — clock (wider now that ✕ is gone)
        self._clock = QLabel("00:00:00", self)
        self._clock.setFont(FONT_CLOCK)
        self._clock.setStyleSheet("color:#ffffff;")
        self._clock.setGeometry(30, 6, W - 44, 36)

        # Row 2 — sync status
        self._status_lbl = QLabel("● synced", self)
        self._status_lbl.setFont(FONT_SMALL)
        self._status_lbl.setStyleSheet("color:#22c55e;")
        self._status_lbl.setGeometry(14, 51, 120, 20)

        # Row 2 — app name
        self._app_lbl = QLabel("", self)
        self._app_lbl.setFont(FONT_APP)
        self._app_lbl.setStyleSheet("color:rgba(255,255,255,128);")
        self._app_lbl.setGeometry(140, 51, W - 154, 20)
        self._app_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    # ── Tray icon ─────────────────────────────────────────────────────────────

    def _setup_tray(self):
        try:
            self._tray = QSystemTrayIcon(QIcon(_dot("#22c55e", 16)), self)
            m = QMenu()
            m.addAction("Show timer", self._show_raise)
            m.addSeparator()
            m.addAction("Restart Computer",  self._restart_computer)
            m.addAction("Shut Down Computer", self._shutdown_computer)
            m.addSeparator()
            m.addAction("Quit TimeTracker (admin)", self._quit_requires_admin)
            self._tray.setContextMenu(m)
            self._tray.activated.connect(self._tray_clicked)
            self._tray.setToolTip("TimeTracker")
            self._tray.show()
        except Exception:
            pass

    def _tray_clicked(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_raise()

    def _show_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_requires_admin(self):
        if sys.platform == "win32":
            from protection.win_admin import confirm_admin_elevation
            ok = confirm_admin_elevation(
                "Quit TimeTracker",
                "Administrator approval is required to stop TimeTracker.\n\n"
                "You will be prompted for an administrator password.",
            )
            if not ok:
                QMessageBox.warning(
                    self,
                    "TimeTracker",
                    "Administrator credentials are required to quit TimeTracker.",
                )
                return
        QApplication.quit()

    # ── Power controls ────────────────────────────────────────────────────────

    def _shutdown_computer(self):
        reply = QMessageBox.question(
            None, "Shut Down", "Save work and shut down this computer?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._shared["db"].end_session(
            self._shared.get("current_session_id"), self._elapsed
        )
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/s", "/t", "5"], check=False)
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to shut down'],
                check=False,
            )
        else:
            subprocess.run(["shutdown", "-h", "+0"], check=False)

    def _restart_computer(self):
        reply = QMessageBox.question(
            None, "Restart", "Save work and restart this computer?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._shared["db"].end_session(
            self._shared.get("current_session_id"), self._elapsed
        )
        if sys.platform == "win32":
            subprocess.run(["shutdown", "/r", "/t", "5"], check=False)
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to restart'],
                check=False,
            )
        else:
            subprocess.run(["shutdown", "-r", "+0"], check=False)

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        t1 = QTimer(self)
        t1.timeout.connect(self._tick_clock)
        t1.start(1000)

        t2 = QTimer(self)
        t2.timeout.connect(self._tick_status)
        t2.start(3000)

        # Check every 5 s if cursor has moved to a different screen
        t3 = QTimer(self)
        t3.timeout.connect(self._tick_screen)
        t3.start(5000)

    def _tick_clock(self):
        now   = int(time.time())
        today = datetime.now().date()

        # ── Load today's accumulated secs once after enrollment ──────────────
        if not self._loaded_today:
            today_secs = self._shared.get("today_active_secs")
            if today_secs is not None:
                self._elapsed      = today_secs
                self._session_date = today
                self._loaded_today = True
                self._last_tick    = now
                h, r = divmod(self._elapsed, 3600)
                m, s = divmod(r, 60)
                self._clock.setText(f"{h:02d}:{m:02d}:{s:02d}")

        # ── New day: midnight rollover or wake to a new calendar day ─────────
        if self._session_date and today > self._session_date:
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            new_id = db.start_session()
            self._shared["current_session_id"] = new_id
            self._elapsed      = 0
            self._session_date = today
            self._last_tick    = now
            self._clock.setText("00:00:00")
            # Clear any pending session-rotation event too
            new_sess = self._shared.get("new_session_event")
            if new_sess:
                new_sess.clear()
            return

        # ── New session event from lock > 1 hour (same day) ─────────────────
        new_sess = self._shared.get("new_session_event")
        if new_sess and new_sess.is_set():
            new_sess.clear()
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            new_id = db.start_session()
            self._shared["current_session_id"] = new_id
            self._elapsed      = 0
            self._session_date = today
            self._last_tick    = now
            self._clock.setText("00:00:00")

        # ── Skip counting while paused (screen locked / idle) ────────────────
        pause = self._shared.get("pause_event")
        if pause and pause.is_set():
            self._last_tick = now   # keep fresh so no spurious gap on resume
            return

        # ── Skip sleep gap (large wall-clock jump without a lock event) ──────
        gap = now - self._last_tick
        self._last_tick = now
        if gap > 10:
            # Machine was asleep — don't count the gap
            return

        self._elapsed += 1
        h, r = divmod(self._elapsed, 3600)
        m, s = divmod(r, 60)
        self._clock.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _tick_status(self):
        status = self._shared.get("sync_status", "syncing")
        if status != self._last_status:
            self._last_status = status
            color = STATUS_COLORS.get(status, "#f59e0b")
            self._status_lbl.setText(STATUS_LABELS.get(status, f"● {status}"))
            self._status_lbl.setStyleSheet(f"color:{color};")
            self._dot_lbl.setPixmap(_dot(color))

        app = self._shared.get("current_app", "")
        if len(app) > 20:
            app = app[:18] + "…"
        if app != self._last_app:
            self._last_app = app
            self._app_lbl.setText(app)

    def _tick_screen(self):
        """Move to top-right of whichever screen the cursor is on."""
        screen = QApplication.screenAt(QCursor.pos())
        if screen and screen != self._last_screen:
            self._move_to_screen(screen)

    # ── Drag ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.LeftButton and not self._drag_pos.isNull():
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = QPoint()

    def closeEvent(self, e):
        # Widget cannot be closed — only hidden via tray or destroyed on quit
        e.ignore()
        self.show()
