"""Unit tests for the PURE meeting detection core (agent/meeting_detect.py).

This is the glitch-free guarantee: presence matching and the session state
machine are exercised here with simulated window/tab snapshots, so the behaviour
the user cares about (no flicker, lock excluded, back-to-back sessions, platform
switches) is verified without installing the agent on a device. The thin OS
collectors that gather the real windows/tabs are device-tested separately."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meeting_detect as md
from meeting_detect import detect_presence, MeetingPresence, MeetingSessionTracker


def W(owner, title=""):
    return {"owner": owner, "title": title}


def T(url, title=""):
    return {"url": url, "title": title}


# ── detect_presence: native call windows ─────────────────────────────────────

def test_zoom_call_window_is_a_meeting():
    p = detect_presence([W("zoom.us", "Zoom Meeting")], [])
    assert p and p.platform == "Zoom"


def test_zoom_app_open_but_not_in_call_is_not_a_meeting():
    # The main Zoom window (no active meeting) must NOT count.
    assert detect_presence([W("zoom.us", "Zoom")], []) is None


def test_teams_call_window_detected_chat_window_not():
    assert detect_presence([W("Microsoft Teams", "Meeting with Acme | Microsoft Teams")], [])
    assert detect_presence([W("Microsoft Teams", "Chat | Microsoft Teams")], []) is None


def test_slack_huddle_detected_normal_slack_not():
    p = detect_presence([W("Slack", "Huddle — #engineering")], [])
    assert p and p.platform == "Slack huddle"
    assert detect_presence([W("Slack", "engineering — Slack")], []) is None


# ── detect_presence: browser in-call tabs ────────────────────────────────────

def test_google_meet_in_call_tab_detected():
    p = detect_presence([], [T("https://meet.google.com/abc-defg-hij", "Meet")])
    assert p and p.platform == "Google Meet"


def test_google_meet_lobby_is_not_a_call():
    assert detect_presence([], [T("https://meet.google.com/", "Meet")]) is None
    assert detect_presence([], [T("https://meet.google.com/landing", "Meet")]) is None


def test_teams_web_meetup_join_detected():
    p = detect_presence([], [T("https://teams.microsoft.com/l/meetup-join/19%3ameeting_x", "Teams")])
    assert p and p.platform == "Microsoft Teams"


def test_zoom_web_client_detected():
    assert detect_presence([], [T("https://us02web.zoom.us/wc/123456/join", "Zoom")])
    assert detect_presence([], [T("https://zoom.us/j/987654321", "Zoom")])


def test_native_takes_priority_over_browser():
    p = detect_presence(
        [W("zoom.us", "Zoom Meeting")],
        [T("https://meet.google.com/abc-defg-hij")],
    )
    assert p.platform == "Zoom"


def test_nothing_open_is_not_a_meeting():
    assert detect_presence([], []) is None
    assert detect_presence([W("Code", "main.py — myproj")], [T("https://github.com")]) is None


# ── MeetingSessionTracker: the glitch-free state machine ─────────────────────

ZOOM = MeetingPresence("Zoom", "Zoom Meeting")
MEET = MeetingPresence("Google Meet", "Standup")


def types(events):
    return [e["type"] for e in events]


def test_start_emitted_once_on_first_presence():
    tr = MeetingSessionTracker(confirm_secs=20)
    assert types(tr.update(ZOOM, now=100)) == ["meeting_start"]
    assert tr.update(ZOOM, now=110) == []   # still present → no duplicate start


def test_brief_disappearance_does_not_end_session():
    tr = MeetingSessionTracker(confirm_secs=20)
    tr.update(ZOOM, now=100)
    assert tr.update(None, now=105) == []   # gone 5s (< 20s) → no end
    assert tr.update(ZOOM, now=110) == []   # came back → same session, no new start


def test_session_ends_after_confirm_window():
    tr = MeetingSessionTracker(confirm_secs=20)
    tr.update(ZOOM, now=100)
    tr.update(ZOOM, now=160)                 # last seen at 160
    assert tr.update(None, now=175) == []    # absent 15s → not yet
    evs = tr.update(None, now=181)           # absent 21s from last-seen → end
    assert types(evs) == ["meeting_end"]
    end = evs[0]
    assert end["platform"] == "Zoom"
    assert end["duration"] == 60             # 160 (last seen) − 100 (start)
    assert end["ts"] == 160                  # ends at last in-call moment


def test_no_spurious_end_when_no_session():
    tr = MeetingSessionTracker(confirm_secs=20)
    assert tr.update(None, now=100) == []
    assert tr.update(None, now=999) == []


def test_platform_switch_mid_session_stays_one_session():
    tr = MeetingSessionTracker(confirm_secs=20)
    assert types(tr.update(ZOOM, now=100)) == ["meeting_start"]
    assert tr.update(MEET, now=130) == []    # switched Zoom→Meet, still in a call
    evs = tr.update(None, now=200)
    assert types(evs) == ["meeting_end"]
    assert evs[0]["platform"] == "Zoom"      # session keeps its original platform
    assert evs[0]["duration"] == 30          # 130 − 100


def test_back_to_back_sessions():
    tr = MeetingSessionTracker(confirm_secs=20)
    assert types(tr.update(ZOOM, now=100)) == ["meeting_start"]
    assert types(tr.update(None, now=130)) == ["meeting_end"]   # 30s absent → end
    assert types(tr.update(MEET, now=300)) == ["meeting_start"] # new meeting
    assert types(tr.update(None, now=340)) == ["meeting_end"]


def test_lock_mid_meeting_ends_at_last_in_call_moment():
    # When the screen locks, the agent stops feeding presence (watchers pause);
    # the tracker then sees only the elapsed clock and must end the session at
    # the last in-call moment so locked time is excluded.
    tr = MeetingSessionTracker(confirm_secs=20)
    tr.update(ZOOM, now=100)
    tr.update(ZOOM, now=200)                 # last in-call moment before lock
    evs = tr.update(None, now=5000)          # unlocked much later
    assert types(evs) == ["meeting_end"]
    assert evs[0]["ts"] == 200
    assert evs[0]["duration"] == 100


# ── WindowWatcher wiring: tracker → DB insert + in_meeting flag ───────────────

def test_window_watcher_persists_meeting_events(monkeypatch):
    """Guards the glue between the pure tracker and the agent: presence → a
    persisted meeting_start/meeting_end event + the shared in_meeting flag that
    idle-exemption reads."""
    import threading
    import capture.window_watcher as ww

    events = []

    class FakeDB:
        def insert_event(self, e):
            events.append(e)

    shared = {
        "db": FakeDB(),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "sync_trigger": threading.Event(),
    }
    w = ww.WindowWatcher(shared)
    monkeypatch.setattr(ww, "_is_media_context", lambda a, t: False)

    # A Zoom call window is present.
    monkeypatch.setattr(ww, "_collect_meeting_signals",
                        lambda: ([{"owner": "zoom.us", "title": "Zoom Meeting"}], []))
    w._update_meetings(now=100, app="zoom.us", title="Zoom Meeting")
    assert any(e["type"] == "meeting_start" and e["app"] == "Zoom" for e in events)
    assert shared["in_meeting"] is True

    # Call ends; after the confirm window a meeting_end is persisted.
    monkeypatch.setattr(ww, "_collect_meeting_signals", lambda: ([], []))
    w._update_meetings(now=100 + ww.MEETING_CONFIRM_SECS + 1, app="Code", title="x")
    assert any(e["type"] == "meeting_end" and e["app"] == "Zoom" for e in events)
    assert shared["in_meeting"] is False
