-- 015_normalize_app_name.sql
--
-- Creates an immutable Postgres function that maps raw agent-reported app names
-- to canonical display names. This is the SQL twin of Python's
-- app/core/app_normalizer.py:normalize_app_name() — both lists must stay in sync.
--
-- Why a function rather than a table UPDATE:
--   activity_events can be very large. A full UPDATE would lock the table and
--   take minutes/hours. Using normalize_app_name() at query time in GROUP BY /
--   JOIN clauses costs nothing extra (it is IMMUTABLE so Postgres caches the
--   result per distinct input) and fixes all historical data instantly.
--
-- After this migration, queries that group or filter by app_name should use
--   normalize_app_name(ae.app_name)
-- instead of the raw column so Chrome / "Google Chrome" variants collapse.

CREATE OR REPLACE FUNCTION normalize_app_name(raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT CASE lower(raw)
    -- ── Chromium-family browsers ────────────────────────────────────────────
    WHEN 'chrome'                   THEN 'Google Chrome'
    WHEN 'google chrome'            THEN 'Google Chrome'
    WHEN 'google-chrome'            THEN 'Google Chrome'
    WHEN 'chromium'                 THEN 'Chromium'
    WHEN 'chromium-browser'         THEN 'Chromium'

    -- ── Microsoft Edge ──────────────────────────────────────────────────────
    WHEN 'msedge'                   THEN 'Microsoft Edge'
    WHEN 'microsoft edge'           THEN 'Microsoft Edge'
    WHEN 'microsoft-edge-stable'    THEN 'Microsoft Edge'
    WHEN 'edge'                     THEN 'Microsoft Edge'

    -- ── Brave ───────────────────────────────────────────────────────────────
    WHEN 'brave'                    THEN 'Brave Browser'
    WHEN 'brave browser'            THEN 'Brave Browser'
    WHEN 'brave-browser'            THEN 'Brave Browser'

    -- ── Other browsers ──────────────────────────────────────────────────────
    WHEN 'firefox'                  THEN 'Firefox'
    WHEN 'firefox-esr'              THEN 'Firefox'
    WHEN 'safari'                   THEN 'Safari'
    WHEN 'arc'                      THEN 'Arc'
    WHEN 'opera'                    THEN 'Opera'
    WHEN 'operagx'                  THEN 'Opera GX'
    WHEN 'opera gx'                 THEN 'Opera GX'
    WHEN 'vivaldi'                  THEN 'Vivaldi'
    WHEN 'vivaldi-stable'           THEN 'Vivaldi'
    WHEN 'iexplore'                 THEN 'Internet Explorer'
    WHEN 'internet explorer'        THEN 'Internet Explorer'

    -- ── Microsoft Office ────────────────────────────────────────────────────
    WHEN 'winword'                  THEN 'Microsoft Word'
    WHEN 'winword.exe'              THEN 'Microsoft Word'
    WHEN 'winword.ex'               THEN 'Microsoft Word'
    WHEN 'microsoft word'           THEN 'Microsoft Word'
    WHEN 'word'                     THEN 'Microsoft Word'

    WHEN 'excel'                    THEN 'Microsoft Excel'
    WHEN 'excel.exe'                THEN 'Microsoft Excel'
    WHEN 'microsoft excel'          THEN 'Microsoft Excel'

    WHEN 'powerpnt'                 THEN 'Microsoft PowerPoint'
    WHEN 'powerpnt.exe'             THEN 'Microsoft PowerPoint'
    WHEN 'microsoft powerpoint'     THEN 'Microsoft PowerPoint'
    WHEN 'powerpoint'               THEN 'Microsoft PowerPoint'

    WHEN 'outlook'                  THEN 'Microsoft Outlook'
    WHEN 'outlook.exe'              THEN 'Microsoft Outlook'
    WHEN 'microsoft outlook'        THEN 'Microsoft Outlook'

    WHEN 'onenote'                  THEN 'Microsoft OneNote'
    WHEN 'microsoft onenote'        THEN 'Microsoft OneNote'

    -- ── Microsoft Teams ─────────────────────────────────────────────────────
    WHEN 'teams'                    THEN 'Microsoft Teams'
    WHEN 'ms teams'                 THEN 'Microsoft Teams'
    WHEN 'microsoft teams'          THEN 'Microsoft Teams'

    -- ── Communication / Productivity ────────────────────────────────────────
    WHEN 'zoom'                     THEN 'Zoom'
    WHEN 'zoom.us'                  THEN 'Zoom'
    WHEN 'us.zoom.videomeetings'    THEN 'Zoom'

    WHEN 'slack'                    THEN 'Slack'
    WHEN 'discord'                  THEN 'Discord'
    WHEN 'skype'                    THEN 'Skype'
    WHEN 'notion'                   THEN 'Notion'
    WHEN 'figma'                    THEN 'Figma'
    WHEN 'spotify'                  THEN 'Spotify'

    -- ── IDEs / editors ──────────────────────────────────────────────────────
    WHEN 'code'                     THEN 'Visual Studio Code'
    WHEN 'code.exe'                 THEN 'Visual Studio Code'
    WHEN 'visual studio code'       THEN 'Visual Studio Code'
    WHEN 'cursor'                   THEN 'Cursor'

    -- ── Terminals ───────────────────────────────────────────────────────────
    WHEN 'iterm2'                   THEN 'iTerm2'
    WHEN 'terminal'                 THEN 'Terminal'
    WHEN 'windowsterminal'          THEN 'Windows Terminal'
    WHEN 'windows terminal'         THEN 'Windows Terminal'
    WHEN 'wt'                       THEN 'Windows Terminal'
    WHEN 'cmd'                      THEN 'Command Prompt'
    WHEN 'powershell'               THEN 'PowerShell'
    WHEN 'gnome-terminal'           THEN 'Terminal'
    WHEN 'konsole'                  THEN 'Terminal'
    WHEN 'xterm'                    THEN 'Terminal'

    ELSE raw   -- unknown names pass through unchanged
  END
$$;

-- Functional index so GROUP BY normalize_app_name(app_name) can use an index scan
-- instead of a seqscan on large activity_events tables.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_activity_events_app_name_normalized
    ON activity_events (normalize_app_name(app_name))
    WHERE app_name IS NOT NULL;
