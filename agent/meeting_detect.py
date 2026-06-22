"""Pure meeting-presence detection + session tracking.

No OS calls, no Qt, no I/O — everything here is decided from in-memory window/tab
snapshots so it can be unit-tested exhaustively (see tests/test_meeting_detect.py).
A thin per-OS shell gathers the real `windows` / `tabs` and feeds them in.

Two pieces:
  • detect_presence(windows, tabs) -> MeetingPresence | None
      Decides whether the user is *in a call right now* (not merely that a
      meeting app is open). Native call windows take priority over browser tabs.
  • MeetingSessionTracker
      Turns a stream of (presence, now) samples into meeting_start / meeting_end
      events, with debounce so brief flicker never opens/closes a session and
      locked/away gaps end the session at the last in-call moment.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class MeetingPresence:
    platform: str   # "Zoom" | "Microsoft Teams" | "Webex" | "Slack huddle" | "Google Meet"
    title: str


# Native call-window signatures: (platform, owner_substr, (title_substrs...)).
# A match needs the owner substring AND any title substring — that title gate is
# what separates an in-call window ("Zoom Meeting") from the app merely being
# open ("Zoom"). Strings are lower-cased before matching. Tunable without code.
NATIVE_CALL_SIGNATURES = (
    ("Zoom",            "zoom",  ("zoom meeting", "zoom webinar")),
    ("Microsoft Teams", "teams", ("meeting", "call", "meetup-join")),
    ("Webex",           "webex", ("meeting",)),
    ("Slack huddle",    "slack", ("huddle",)),
)

# Browser in-call URL signatures: (platform, regex). These match the *in-call*
# URL, not the product landing page — e.g. a Meet room code, a Teams meetup-join
# link, a Zoom web-client/join path.
BROWSER_CALL_PATTERNS = (
    ("Google Meet",     re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)),
    ("Microsoft Teams", re.compile(r"teams\.(microsoft|live)\.com/.*meetup-join", re.I)),
    ("Zoom",            re.compile(r"zoom\.us/(j|wc)/\d", re.I)),
    ("Webex",           re.compile(r"\.webex\.com/(meet|join)/", re.I)),
)


def detect_presence(windows, tabs=()):
    """Return a MeetingPresence if the user is in a call right now, else None.

    windows: iterable of {"owner": str, "title": str} for visible windows.
    tabs:    iterable of {"url": str, "title": str} for open browser tabs.
    """
    for w in windows or ():
        owner = (w.get("owner") or "").lower()
        title = (w.get("title") or "").lower()
        for platform, owner_sub, title_subs in NATIVE_CALL_SIGNATURES:
            if owner_sub in owner and any(t in title for t in title_subs):
                return MeetingPresence(platform, w.get("title") or platform)

    for t in tabs or ():
        url = t.get("url") or ""
        for platform, rx in BROWSER_CALL_PATTERNS:
            if rx.search(url):
                return MeetingPresence(platform, t.get("title") or platform)

    return None


class MeetingSessionTracker:
    """Stateful: feed it (presence, now) each poll; it yields session events.

    Debounce model (glitch-free):
      • start  — emitted the first time presence is seen.
      • continue — while presence keeps being seen the session stays open; a gap
        shorter than confirm_secs is ignored (no flicker), and a platform switch
        keeps the same session.
      • end    — emitted once presence has been absent for >= confirm_secs; the
        session is dated to the *last in-call moment* (last_present_ts), so the
        trailing confirm window — and any locked/away gap — is excluded.
    """

    def __init__(self, confirm_secs: float = 20.0):
        self.confirm_secs = confirm_secs
        self._active = None              # MeetingPresence | None
        self._start_ts = None            # float
        self._last_present_ts = None     # float

    @property
    def in_meeting(self) -> bool:
        return self._active is not None

    def update(self, presence, now: float):
        """Advance the state machine one sample. Returns a list of event dicts
        (usually empty). Event dicts: {type, platform, title, ts[, duration]}."""
        events = []

        if presence is not None:
            if self._active is None:
                self._active = presence
                self._start_ts = now
                events.append({
                    "type": "meeting_start",
                    "platform": presence.platform,
                    "title": presence.title,
                    "ts": now,
                })
            # Keep the session's original platform/title; just note we're still in.
            self._last_present_ts = now
            return events

        # presence is None
        if self._active is not None and self._last_present_ts is not None:
            if now - self._last_present_ts >= self.confirm_secs:
                events.append({
                    "type": "meeting_end",
                    "platform": self._active.platform,
                    "title": self._active.title,
                    "ts": self._last_present_ts,
                    "duration": int(self._last_present_ts - self._start_ts),
                })
                self._active = None
                self._start_ts = None
                self._last_present_ts = None
        return events
