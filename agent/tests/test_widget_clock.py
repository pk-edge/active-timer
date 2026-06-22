"""The active-time clock must count LIVE activity only — the fix for inflated
'active' time (e.g. a machine left unlocked and idle counting as active)."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from widget.floating_widget import _clock_advances


def test_counts_one_live_second():
    assert _clock_advances(paused=False, is_idle=False, gap_secs=1) is True


def test_stops_when_locked_or_asleep():
    assert _clock_advances(paused=True, is_idle=False, gap_secs=1) is False


def test_stops_when_idle():
    # Core fix: no live input (idle) must NOT count, even while unlocked.
    # (InputMonitor exempts idle during meetings/media, so live meetings still count.)
    assert _clock_advances(paused=False, is_idle=True, gap_secs=1) is False


def test_does_not_jump_across_suspend_gap():
    # Lid-close / system sleep suspends the process; on resume never jump forward.
    assert _clock_advances(paused=False, is_idle=False, gap_secs=3600) is False
