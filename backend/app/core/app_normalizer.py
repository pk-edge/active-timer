"""
App name normalization for cross-platform consistency.

Problem: the agent reports the OS-level process/app name, which varies by platform:
  - macOS  NSWorkspace.localizedName() → "Google Chrome", "Microsoft Edge"
  - Windows psutil.Process.name()       → "chrome",        "msedge"
  - Linux   psutil.Process.name()       → "google-chrome", "microsoft-edge-stable"

These land in activity_events.app_name as different strings for the same app, which
splits every GROUP BY (top-apps, category join, reports) across platform variants.

Fix:
  normalize_app_name() is applied at ingest time (Python) so all new rows are clean.
  A companion Postgres function (migration 015_normalize_app_name.sql) applies the
  same mapping at query time so historical rows are also normalized without a full
  table rewrite — GROUP BY normalize_app_name(ae.app_name) collapses the variants.

Maintenance: add new entries here AND to the Postgres CASE in the migration.
Both lists must be kept in sync (the migration comment points back here).
"""
from __future__ import annotations

# Maps raw agent-reported name (lowercased) → canonical display name.
# Only entries where real-world variance has been observed are listed.
# Unrecognized names pass through unchanged (title-cased for presentation).
_CANONICAL: dict[str, str] = {
    # ── Chromium-family browsers ─────────────────────────────────────────────
    "chrome":               "Google Chrome",   # Windows process name
    "google chrome":        "Google Chrome",   # macOS / Linux localizedName
    "google-chrome":        "Google Chrome",   # Linux process name
    "chromium":             "Chromium",
    "chromium-browser":     "Chromium",

    # ── Microsoft Edge ───────────────────────────────────────────────────────
    "msedge":               "Microsoft Edge",  # Windows process name
    "microsoft edge":       "Microsoft Edge",  # macOS localizedName
    "microsoft-edge-stable":"Microsoft Edge",  # Linux process name
    "edge":                 "Microsoft Edge",

    # ── Brave ────────────────────────────────────────────────────────────────
    "brave":                "Brave Browser",   # Windows process name
    "brave browser":        "Brave Browser",   # macOS localizedName
    "brave-browser":        "Brave Browser",   # Linux process name

    # ── Other browsers ───────────────────────────────────────────────────────
    "firefox":              "Firefox",
    "firefox-esr":          "Firefox",
    "safari":               "Safari",
    "arc":                  "Arc",
    "opera":                "Opera",
    "operagx":              "Opera GX",
    "opera gx":             "Opera GX",
    "vivaldi":              "Vivaldi",
    "vivaldi-stable":       "Vivaldi",
    "iexplore":             "Internet Explorer",
    "internet explorer":    "Internet Explorer",

    # ── Microsoft Office — Windows exe names (uppercase after .exe strip) ───
    # psutil returns "WINWORD.EXE" → agent strips .exe → "WINWORD.EX" (bug in
    # agent, case-sensitive strip); we handle both the stripped and full forms.
    "winword":              "Microsoft Word",
    "winword.exe":          "Microsoft Word",
    "winword.ex":           "Microsoft Word",
    "microsoft word":       "Microsoft Word",
    "word":                 "Microsoft Word",

    "excel":                "Microsoft Excel",
    "excel.exe":            "Microsoft Excel",
    "microsoft excel":      "Microsoft Excel",

    "powerpnt":             "Microsoft PowerPoint",
    "powerpnt.exe":         "Microsoft PowerPoint",
    "microsoft powerpoint": "Microsoft PowerPoint",
    "powerpoint":           "Microsoft PowerPoint",

    "outlook":              "Microsoft Outlook",
    "outlook.exe":          "Microsoft Outlook",
    "microsoft outlook":    "Microsoft Outlook",

    "onenote":              "Microsoft OneNote",
    "microsoft onenote":    "Microsoft OneNote",

    # ── Microsoft Teams ──────────────────────────────────────────────────────
    "teams":                "Microsoft Teams",
    "ms teams":             "Microsoft Teams",
    "microsoft teams":      "Microsoft Teams",

    # ── Communication / Productivity ─────────────────────────────────────────
    "zoom":                 "Zoom",
    "zoom.us":              "Zoom",
    "us.zoom.videomeetings":"Zoom",

    "slack":                "Slack",
    "discord":              "Discord",
    "skype":                "Skype",

    "notion":               "Notion",
    "figma":                "Figma",
    "spotify":              "Spotify",

    # ── IDEs / editors ───────────────────────────────────────────────────────
    # macOS / Linux: process name is "code"; macOS localizedName is "Visual Studio Code"
    "code":                 "Visual Studio Code",
    "code.exe":             "Visual Studio Code",
    "visual studio code":   "Visual Studio Code",

    "cursor":               "Cursor",

    # ── Terminals ────────────────────────────────────────────────────────────
    "iterm2":               "iTerm2",
    "terminal":             "Terminal",
    "windowsterminal":      "Windows Terminal",
    "windows terminal":     "Windows Terminal",
    "wt":                   "Windows Terminal",
    "cmd":                  "Command Prompt",
    "powershell":           "PowerShell",
    "gnome-terminal":       "Terminal",
    "konsole":              "Terminal",
    "xterm":                "Terminal",
}


def normalize_app_name(app: str | None) -> str | None:
    """Return the canonical display name for an app.

    Normalizes cross-platform variants (e.g. "chrome" → "Google Chrome") so
    the same app always groups together in reports regardless of which OS or
    agent version reported it. Unknown names are returned as-is (not lowercased,
    not modified) so no data is silently lost.
    """
    if not app:
        return app
    return _CANONICAL.get(app.lower(), app)
