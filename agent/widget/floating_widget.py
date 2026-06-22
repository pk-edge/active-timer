"""
FloatingWidget — Daylight theme, exact SVG colour scheme.

Card: 270×140 px, #FCFBF9 at 88 % opacity (solid enough to read, hint of glass).
Clock: large #6D1C95 purple HH:MM + dimmer :SS.
Progress bar, green accents, muted labels — all from the SVG palette.
"""

import json
import logging
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QLabel, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QSystemTrayIcon, QMenu, QApplication, QMessageBox,
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRectF, QEvent,
)
from PySide6.QtGui import (
    QFont, QColor, QPainter, QPainterPath, QPen,
    QIcon, QPixmap, QBrush,
)

# ── Geometry ──────────────────────────────────────────────────────────────────
PAD    = 12          # transparent shadow zone
W_CARD = 270
H_CARD = 140
W      = W_CARD + PAD * 2   # 294
H      = H_CARD + PAD * 2   # 164
RADIUS = 16

log = logging.getLogger("floating_widget")

DAILY_GOAL_SECS = 8 * 3600


def _clock_advances(paused: bool, is_idle: bool, gap_secs: int) -> bool:
    """Whether the active-time clock should count this second — LIVE activity only.

    Stops while the screen is locked/asleep (paused), while the user is idle (no
    live input — note InputMonitor EXEMPTS idle during meetings/media, so live
    meetings/video keep counting), and across a large gap (process was suspended
    by system sleep / lid-close, so we must never jump the counter forward)."""
    if paused or is_idle:
        return False
    return gap_secs <= 10

# ── SVG palette (exact) ───────────────────────────────────────────────────────
CARD_BG     = QColor(0xFC, 0xFB, 0xF9, 224)   # #FCFBF9 @ 88 % — readable, light glass
CARD_BORDER = QColor(0xEB, 0xE5, 0xDC, 255)   # #EBE5DC

_C_HDR      = "#94897E"   # ACTIVE TODAY label
_C_CLOK     = "#6D1C95"   # HH:MM — deep purple
_C_CLOK_DIM = "#B09CC0"   # :SS  — purple @ ~50 % (SVG purple-dim)
_C_GOAL_LBL = "#A8A096"   # "8hrs Daily Goal" / info label
_C_DATE     = "#79716A"   # date footer left
_C_PROG_BG  = "#EDE8E1"   # progress track
_C_PROG_FG  = "#1C9540"   # progress fill + status green

C_STATUS_MAP = {
    "ok":        QColor("#1C9540"),
    "syncing":   QColor("#3b82f6"),
    "offline":   QColor("#ef4444"),
    "enrolling": QColor("#06b6d4"),
    "error":     QColor("#ef4444"),
}
STATUS_LABELS = {
    "ok":        "synced",
    "syncing":   "syncing…",
    "offline":   "offline",
    "enrolling": "connecting…",
    "error":     "error",
}

# ── Fonts ─────────────────────────────────────────────────────────────────────
if sys.platform == "darwin":
    _fam     = "Helvetica Neue"
    _sz_big  = 28      # HH:MM
    _sz_secs = 18      # :SS  (≈ 0.66 × 28)
    _sz_ui   = 10
else:
    _fam     = "Segoe UI"
    _sz_big  = 22
    _sz_secs = 15
    _sz_ui   = 9

FONT_UI = QFont(_fam, _sz_ui)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Widget ────────────────────────────────────────────────────────────────────

class FloatingWidget(QWidget):
    def __init__(self, shared: dict):
        super().__init__()
        self._shared       = shared
        self._elapsed      = 0
        self._drag_pos     = QPoint()
        self._last_status  = ""
        self._is_idle      = False
        self._status_clr   = C_STATUS_MAP["ok"]
        self._progress     = 0.0

        self._loaded_today   = False   # True once first value loaded from shared
        self._correction_done = False  # True once server-reconciled value applied
        self._init_unix      = int(time.time())
        self._session_date   = datetime.now().date()
        self._last_tick      = int(time.time())
        self._allow_close    = False   # True when an OS shutdown/restart is in progress

        self._setup_window()
        self._build_ui()
        self._setup_tray()
        self._start_timers()
        self._hook_session_end()

    # ── OS shutdown/restart must never be blocked ─────────────────────────────
    # The widget normally vetoes close events (the timer hides instead of
    # quitting), but the SAME close path is used by Windows logoff/shutdown and
    # by macOS termination — vetoing there makes the OS report "TimeTracker is
    # preventing shutdown". These hooks detect a session end and lift the veto.

    def _hook_session_end(self):
        app = QApplication.instance()
        if app is None:
            return
        try:
            # Windows/X11: emitted while the OS is committing the session.
            app.commitDataRequest.connect(self._on_session_end)
        except Exception:
            pass
        # macOS: QEvent.Quit is NOT reliably delivered to the app before the
        # window close during a real shutdown (applicationShouldTerminate sends
        # close events directly), so this filter alone let the close be vetoed
        # and the OS reported the agent as preventing shutdown. The reliable
        # signal is NSWorkspace's power-off notification (logout/restart/shutdown).
        app.installEventFilter(self)
        if sys.platform == "darwin":
            try:
                from AppKit import NSWorkspace
                nc = NSWorkspace.sharedWorkspace().notificationCenter()
                # Keep the returned token on self so the observer isn't GC'd.
                self._poweroff_observer = nc.addObserverForName_object_queue_usingBlock_(
                    "NSWorkspaceWillPowerOffNotification", None, None,
                    lambda _n: self._on_macos_power_off(),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("macOS power-off observer setup failed: %s", e)

    def _on_session_end(self, *_a):
        self._allow_close = True

    def _on_macos_power_off(self, *_a):
        # Logout / restart / shutdown requested: lift the close veto and quit
        # promptly so the agent never blocks the power action. aboutToQuit runs
        # the final daily flush + session end; autostart relaunches on next login.
        log.info("macOS power-off/logout detected — quitting agent cleanly")
        self._allow_close = True
        QApplication.quit()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Quit:
            self._allow_close = True
        return super().eventFilter(obj, ev)

    @property
    def elapsed(self) -> int:
        return self._elapsed

    @property
    def session_date(self):
        return self._session_date

    # ── Window ────────────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Window
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)

        screen = QApplication.primaryScreen()
        saved  = _load_pos()
        if saved and screen:
            g = screen.availableGeometry()
            x = max(g.left(), min(saved.x(), g.right()  - W))
            y = max(g.top(),  min(saved.y(), g.bottom() - H))
            self.move(x, y)
        elif screen:
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

    # ── Paint: card, shadow, progress bar, dots ───────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # ── Two-layer drop shadow (SVG filter0_dd values) ─────────────────────
        sh1 = QPainterPath()
        sh1.addRoundedRect(QRectF(PAD - 1, PAD + 2, W_CARD + 2, H_CARD + 1), RADIUS, RADIUS)
        p.fillPath(sh1, QColor(0x2A, 0x1A, 0x4A, 26))   # near, tight

        for i in range(5, 0, -1):
            sh2 = QPainterPath()
            sh2.addRoundedRect(
                QRectF(PAD - i, PAD + i * 2, W_CARD + i * 2, H_CARD + i),
                RADIUS + i, RADIUS + i,
            )
            p.fillPath(sh2, QColor(0x2A, 0x1A, 0x4A, i * 2))  # deep, diffuse

        # ── Card fill ─────────────────────────────────────────────────────────
        card = QPainterPath()
        card.addRoundedRect(QRectF(PAD, PAD, W_CARD, H_CARD), RADIUS, RADIUS)
        p.setClipPath(card)
        p.fillPath(card, CARD_BG)
        p.setClipping(False)

        # ── Card border (#EBE5DC) ─────────────────────────────────────────────
        p.setPen(QPen(CARD_BORDER, 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(card)

        # ── Header dot — green (#1C9540) or amber when idle ───────────────────
        dot_clr = QColor("#f59e0b") if self._is_idle else C_STATUS_MAP["ok"]
        p.setBrush(dot_clr)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(PAD + 20 - 3.5, PAD + 18 - 3.5, 7.0, 7.0))

        # ── Progress bar track (#EDE8E1) ──────────────────────────────────────
        pb_x = PAD + 18
        pb_y = PAD + 100
        pb_w = W_CARD - 36   # 234 px
        pb_h = 10
        track = QPainterPath()
        track.addRoundedRect(QRectF(pb_x, pb_y, pb_w, pb_h), pb_h / 2, pb_h / 2)
        p.fillPath(track, QColor(_C_PROG_BG))

        # ── Progress fill (#1C9540) + SVG filter2_d glow ─────────────────────
        fill_w = max(pb_h, int(pb_w * self._progress))
        glow = QPainterPath()
        glow.addRoundedRect(
            QRectF(pb_x - 2, pb_y - 2, fill_w + 4, pb_h + 4),
            (pb_h + 4) / 2, (pb_h + 4) / 2,
        )
        p.fillPath(glow, QColor(0x14, 0xB8, 0xA6, 45))  # teal glow

        fill = QPainterPath()
        fill.addRoundedRect(QRectF(pb_x, pb_y, fill_w, pb_h), pb_h / 2, pb_h / 2)
        p.fillPath(fill, QColor(_C_PROG_FG))

        # ── Footer sync dot ───────────────────────────────────────────────────
        p.setBrush(self._status_clr)
        p.setPen(Qt.NoPen)
        # sits just left of the status label
        lbl_right = PAD + W_CARD - 18
        p.drawEllipse(QRectF(lbl_right - 68 - 9, PAD + 122 - 3.5, 7.0, 7.0))

        p.end()

    # ── Labels ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header: "ACTIVE TODAY" (#94897E) — card-rel x=32, y=10, h=16
        self._hdr_lbl = QLabel("ACTIVE TODAY", self)
        self._hdr_lbl.setFont(FONT_UI)
        self._hdr_lbl.setStyleSheet(
            f"color:{_C_HDR}; background:transparent; font-weight:600; letter-spacing:2px;"
        )
        self._hdr_lbl.setGeometry(PAD + 32, PAD + 10, 200, 16)
        self._hdr_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # ── Clock: HH:MM large + :SS dim — card-rel y=30, h=60 ───────────────
        self._clock_lbl = QLabel(self)
        self._clock_lbl.setTextFormat(Qt.RichText)
        self._clock_lbl.setGeometry(PAD, PAD + 28, W_CARD, 60)
        self._clock_lbl.setAlignment(Qt.AlignCenter)
        self._clock_lbl.setStyleSheet("background:transparent;")

        # Subtle purple glow matching SVG filter1_d
        glow = QGraphicsDropShadowEffect(self._clock_lbl)
        glow.setBlurRadius(20)
        glow.setColor(QColor(0x7C, 0x3A, 0xED, 38))
        glow.setOffset(0, 0)
        self._clock_lbl.setGraphicsEffect(glow)

        # ── Goal row: "8hrs Daily Goal" (#A8A096) left — card-rel y=90, h=13
        self._goal_lbl = QLabel("8hrs · daily goal", self)
        self._goal_lbl.setFont(FONT_UI)
        self._goal_lbl.setStyleSheet(
            f"color:{_C_GOAL_LBL}; background:transparent; font-weight:500;"
        )
        self._goal_lbl.setGeometry(PAD + 18, PAD + 88, 140, 13)
        self._goal_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # ── Goal %: (#1C9540) right — same row
        self._pct_lbl = QLabel("0%", self)
        self._pct_lbl.setFont(FONT_UI)
        self._pct_lbl.setStyleSheet(
            f"color:{_C_PROG_FG}; background:transparent; font-weight:700;"
        )
        self._pct_lbl.setGeometry(PAD + W_CARD - 18 - 42, PAD + 88, 42, 13)
        self._pct_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # ── Date (#79716A) — card-rel y=118, h=13
        self._date_lbl = QLabel(_date_str(), self)
        self._date_lbl.setFont(FONT_UI)
        self._date_lbl.setStyleSheet(
            f"color:{_C_DATE}; background:transparent; font-weight:500;"
        )
        self._date_lbl.setGeometry(PAD + 18, PAD + 116, 70, 13)
        self._date_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        # ── Status text (#1C9540) — right side, after painted dot
        self._status_lbl = QLabel("synced", self)
        self._status_lbl.setFont(FONT_UI)
        self._status_lbl.setStyleSheet(
            f"color:{_C_PROG_FG}; background:transparent; font-weight:700;"
        )
        self._status_lbl.setGeometry(PAD + W_CARD - 18 - 60, PAD + 116, 60, 13)
        self._status_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # Pulse animation on status label (SVG dot--pulse equivalent)
        self._dot_opacity = QGraphicsOpacityEffect(self._status_lbl)
        self._dot_opacity.setOpacity(1.0)
        self._status_lbl.setGraphicsEffect(self._dot_opacity)

        self._dot_anim = QPropertyAnimation(self._dot_opacity, b"opacity", self)
        self._dot_anim.setDuration(2200)
        self._dot_anim.setStartValue(1.0)
        self._dot_anim.setEndValue(0.25)
        self._dot_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._dot_anim.setLoopCount(-1)
        self._dot_anim.start()

        # Initial draw
        self._redraw()

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _setup_tray(self):
        try:
            self._tray = QSystemTrayIcon(_tray_icon(C_STATUS_MAP["ok"]), self)
            m = QMenu()
            m.addAction("Show timer",               self._show_raise)
            m.addSeparator()
            m.addAction("Restart Computer",         self._restart_computer)
            m.addAction("Shut Down Computer",       self._shutdown_computer)
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

    # Restart/shutdown are available to EVERY user — admin and non-admin alike.
    # Only quitting the agent is admin-gated (see _quit_requires_admin); the
    # power actions ask a plain yes/no confirmation and never prompt for
    # elevation (Windows grants SeShutdownPrivilege to standard users, and the
    # macOS System Events route needs no privileges beyond the one-time
    # Automation consent).

    def _shutdown_computer(self):
        if QMessageBox.question(None, "Shut Down", "Shut down this computer?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._power_command(restart=False)

    def _restart_computer(self):
        if QMessageBox.question(None, "Restart", "Restart this computer?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._power_command(restart=True)

    def _power_command(self, *, restart: bool) -> None:
        """Issue the OS restart/shutdown, then quit the agent immediately.

        Fire-and-forget (Popen, NOT run): on macOS the AppleScript round-trip
        only returns once every app — including this one — has quit, so a
        blocking call on the Qt main thread deadlocks the shutdown. Quitting
        right after gives the OS a clean exit (session end + daily flush run
        in the aboutToQuit handler) and guarantees the agent never blocks the
        power action it just requested. Autostart brings it back on login.
        """
        if sys.platform == "win32":
            cmd = ["shutdown", "/r" if restart else "/s", "/t", "5"]
        elif sys.platform == "darwin":
            verb = "restart" if restart else "shut down"
            cmd = ["osascript", "-e", f'tell application "System Events" to {verb}']
        else:
            cmd = ["shutdown", "-r" if restart else "-h", "+0"]
        try:
            subprocess.Popen(cmd)
        except Exception:
            QMessageBox.warning(self, "TimeTracker",
                "Could not start the power action — please use the system menu.")
            return
        self._allow_close = True
        QApplication.quit()

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        self._t1 = QTimer(self); self._t1.timeout.connect(self._tick_clock);  self._t1.start(1000)
        self._t2 = QTimer(self); self._t2.timeout.connect(self._tick_status); self._t2.start(3000)

    # ── Tick: clock ───────────────────────────────────────────────────────────

    def _tick_clock(self):
        now   = int(time.time())
        today = datetime.now().date()

        # ── Stage 1: wait for pre-loaded local value (set immediately in main.py) ─
        if not self._loaded_today:
            ts = self._shared.get("today_active_secs")
            if ts is None:
                return  # still waiting — show 00:00:00
            self._elapsed      = ts
            self._session_date = today
            self._loaded_today = True
            self._last_tick    = now
            self._redraw()
            return

        # ── Stage 2: accept server-reconciled correction within 90s of launch ──
        # Enrollment may update today_active_secs to a higher server value after
        # the local pre-load. Apply it once if the server value is larger.
        if not self._correction_done:
            ts = self._shared.get("today_active_secs")
            if ts is not None and ts > self._elapsed + 30:
                self._elapsed   = ts
                self._last_tick = now
                self._correction_done = True
                self._redraw()
                return
            # Close the correction window after 90s — enrollment is long done
            if now - self._init_unix > 90:
                self._correction_done = True

        # ── Day rollover — reset elapsed and start a new attendance session ──
        if self._session_date and today > self._session_date:
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            nid = db.start_session()
            self._shared["current_session_id"] = nid
            self._elapsed = 0; self._session_date = today; self._last_tick = now
            s = self._shared.get("new_session_event")
            if s: s.clear()
            self._redraw()
            return

        s = self._shared.get("new_session_event")
        if s and s.is_set():
            s.clear()
            db = self._shared["db"]
            db.end_session(self._shared.get("current_session_id"), self._elapsed)
            nid = db.start_session()
            self._shared["current_session_id"] = nid
            self._elapsed = 0; self._session_date = today; self._last_tick = now
            self._redraw()
            return

        pause  = self._shared.get("pause_event")
        paused = bool(pause and pause.is_set())
        idle   = bool(self._shared.get("is_idle"))   # set by InputMonitor (idle-exempt in meetings)

        gap = now - self._last_tick
        self._last_tick = now
        # Count only LIVE activity: not locked/asleep, not idle, no suspend gap.
        if _clock_advances(paused, idle, gap):
            self._elapsed += 1
            self._redraw()

    def _redraw(self):
        h, r = divmod(self._elapsed, 3600)
        m, s = divmod(r, 60)
        big = (f"font-family:{_fam}; font-size:{_sz_big}pt;"
               f" font-weight:800; color:{_C_CLOK};")
        dim = (f"font-family:{_fam}; font-size:{_sz_secs}pt;"
               f" font-weight:800; color:{_C_CLOK_DIM};")
        self._clock_lbl.setText(
            f'<span style="{big}">{h:02d}:{m:02d}</span>'
            f'<span style="{dim}">:{s:02d}</span>'
        )
        self._progress = min(1.0, self._elapsed / DAILY_GOAL_SECS)
        self._pct_lbl.setText(f"{int(self._progress * 100)}%")
        self.update()

    # ── Tick: status ──────────────────────────────────────────────────────────

    def _tick_status(self):
        self._date_lbl.setText(_date_str())

        pause   = self._shared.get("pause_event")
        is_idle = bool((pause and pause.is_set()) or self._shared.get("is_idle"))
        if is_idle != self._is_idle:
            self._is_idle = is_idle
            self._hdr_lbl.setText("IDLE" if is_idle else "ACTIVE TODAY")
            self._hdr_lbl.setStyleSheet(
                f"color:{'#f59e0b' if is_idle else _C_HDR}; background:transparent;"
                f" font-weight:600; letter-spacing:2px;"
            )
            self.update()

        status = self._shared.get("sync_status", "syncing")
        if status != self._last_status:
            self._last_status = status
            self._status_clr  = C_STATUS_MAP.get(status, C_STATUS_MAP["ok"])
            clr = self._status_clr.name()
            self._status_lbl.setText(STATUS_LABELS.get(status, status))
            self._status_lbl.setStyleSheet(
                f"color:{clr}; background:transparent; font-weight:700;"
            )
            self._tray.setIcon(_tray_icon(self._status_clr))
            self.update()

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
        app = QApplication.instance()
        session_ending = bool(app and app.isSavingSession())
        if self._allow_close or session_ending:
            e.accept()           # OS shutdown/restart/logoff — never stand in the way
            return
        e.ignore()               # casual Alt+F4 / ⌘W — keep the timer alive
        self.show()
