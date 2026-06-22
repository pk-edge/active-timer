"""Idle policy: live meeting/video time is NEVER cut by inactivity.

A meeting/media context exempts idle indefinitely — the user may be watching or
listening. A walk-away during a meeting is bounded by screen lock / display sleep,
not by an input timeout (stakeholder decision). Outside a meeting, normal 5-minute
input idle still applies."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config import Config
from capture.input_monitor import _should_mark_idle

IDLE = 300    # IDLE_THRESHOLD_SECS (5 min)


def test_not_idle_before_threshold():
    assert _should_mark_idle(120, exempt=False, idle_threshold=IDLE) is False


def test_idle_after_threshold_when_not_exempt():
    assert _should_mark_idle(301, exempt=False, idle_threshold=IDLE) is True


def test_meeting_or_media_is_never_marked_idle():
    # Live meeting/video must never be cut by inactivity — at 5 min, 1 hour, 15 hours.
    assert _should_mark_idle(600, exempt=True, idle_threshold=IDLE) is False
    assert _should_mark_idle(3600, exempt=True, idle_threshold=IDLE) is False
    assert _should_mark_idle(15 * 3600, exempt=True, idle_threshold=IDLE) is False


def test_idle_threshold_is_the_crucial_value():
    # Stats depend on this exact non-meeting idle threshold — pin it.
    assert Config.IDLE_THRESHOLD_SECS == 300
