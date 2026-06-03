"use client";
import { useState } from "react";
import { useAuth } from "@/hooks/useAuth";
import {
  Building2, Camera, Activity, Bell,
  Check, ChevronRight,
} from "lucide-react";

type Tab = "general" | "tracking" | "screenshots" | "notifications";

const TABS: { key: Tab; label: string; icon: React.ReactNode }[] = [
  { key: "general",       label: "General",       icon: <Building2 size={16} /> },
  { key: "tracking",      label: "Tracking",      icon: <Activity  size={16} /> },
  { key: "screenshots",   label: "Screenshots",   icon: <Camera    size={16} /> },
  { key: "notifications", label: "Notifications", icon: <Bell      size={16} /> },
];

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none ${
        checked ? "bg-blue-600" : "bg-gray-300"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function SettingRow({
  label, description, children,
}: {
  label: string; description?: string; children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between py-4 border-b border-gray-100 last:border-0">
      <div className="flex-1 min-w-0 pr-8">
        <p className="text-sm font-medium text-gray-800">{label}</p>
        {description && <p className="text-xs text-gray-500 mt-0.5">{description}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function SelectInput({ value, onChange, options }: {
  value: string; onChange: (v: string) => void;
  options: { label: string; value: string }[];
}) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-blue-400"
    >
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

export default function SettingsPage() {
  useAuth();
  const [tab, setTab] = useState<Tab>("general");
  const [saved, setSaved] = useState(false);

  // General
  const [orgName,   setOrgName]   = useState("My Organization");
  const [timezone,  setTimezone]  = useState("UTC");
  const [workStart, setWorkStart] = useState("09:00");
  const [workEnd,   setWorkEnd]   = useState("18:00");

  // Tracking
  const [trackUrls,       setTrackUrls]       = useState(true);
  const [trackWindowTitles, setTrackWindowTitles] = useState(true);
  const [idleTimeout,     setIdleTimeout]     = useState("5");
  const [trackKeyboard,   setTrackKeyboard]   = useState(false);

  // Screenshots
  const [screenshotsEnabled, setScreenshotsEnabled] = useState(true);
  const [screenshotInterval, setScreenshotInterval] = useState("10");
  const [blurScreenshots,    setBlurScreenshots]    = useState(false);
  const [screenshotQuality,  setScreenshotQuality]  = useState("medium");

  // Notifications
  const [emailAlerts,      setEmailAlerts]      = useState(true);
  const [idleAlertMins,    setIdleAlertMins]    = useState("30");
  const [offlineAlertMins, setOfflineAlertMins] = useState("15");
  const [dailyReport,      setDailyReport]      = useState(true);

  function handleSave() {
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  }

  return (
    <div>
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900 mb-1">Settings</h1>
          <p className="text-gray-500 text-sm">Configure tracking, screenshots, and notifications</p>
        </div>
        <button
          onClick={handleSave}
          className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
            saved
              ? "bg-green-50 text-green-600 border border-green-200"
              : "bg-blue-600 text-white hover:bg-blue-700"
          }`}
        >
          {saved ? <><Check size={14} /> Saved</> : "Save changes"}
        </button>
      </div>

      <div className="flex gap-6">
        {/* Sidebar nav */}
        <div className="w-48 shrink-0">
          <nav className="space-y-0.5">
            {TABS.map(t => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex items-center justify-between w-full px-3 py-2.5 rounded-lg text-sm transition ${
                  tab === t.key
                    ? "bg-blue-50 text-blue-700 font-medium"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                }`}
              >
                <span className="flex items-center gap-2.5">
                  {t.icon}
                  {t.label}
                </span>
                {tab === t.key && <ChevronRight size={13} className="text-blue-400" />}
              </button>
            ))}
          </nav>
        </div>

        {/* Content panel */}
        <div className="flex-1 bg-white border border-gray-200 rounded-xl shadow-sm p-6">

          {/* ── General ─────────────────────────────────────────── */}
          {tab === "general" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">General</h2>
              <p className="text-sm text-gray-500 mb-5">Organization details and working hours</p>

              <SettingRow label="Organization name" description="Displayed in reports and notifications">
                <input
                  type="text"
                  value={orgName}
                  onChange={e => setOrgName(e.target.value)}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 w-52 focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-blue-400"
                />
              </SettingRow>

              <SettingRow label="Timezone" description="Used for all time displays and reports">
                <SelectInput
                  value={timezone}
                  onChange={setTimezone}
                  options={[
                    { label: "UTC", value: "UTC" },
                    { label: "Asia/Karachi (PKT +5:00)", value: "Asia/Karachi" },
                    { label: "America/New_York (EST)", value: "America/New_York" },
                    { label: "America/Los_Angeles (PST)", value: "America/Los_Angeles" },
                    { label: "Europe/London (GMT)", value: "Europe/London" },
                    { label: "Asia/Dubai (GST +4:00)", value: "Asia/Dubai" },
                  ]}
                />
              </SettingRow>

              <SettingRow label="Work hours start" description="Start of the standard working day">
                <input
                  type="time"
                  value={workStart}
                  onChange={e => setWorkStart(e.target.value)}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-blue-400"
                />
              </SettingRow>

              <SettingRow label="Work hours end" description="End of the standard working day">
                <input
                  type="time"
                  value={workEnd}
                  onChange={e => setWorkEnd(e.target.value)}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-blue-400"
                />
              </SettingRow>
            </div>
          )}

          {/* ── Tracking ─────────────────────────────────────────── */}
          {tab === "tracking" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Tracking</h2>
              <p className="text-sm text-gray-500 mb-5">Control what activity data the agent captures</p>

              <SettingRow label="Track active applications" description="Record which app is in focus and for how long">
                <Toggle checked={true} onChange={() => {}} />
              </SettingRow>

              <SettingRow label="Track window titles" description="Capture the title bar text of the active window">
                <Toggle checked={trackWindowTitles} onChange={setTrackWindowTitles} />
              </SettingRow>

              <SettingRow label="Track browser URLs" description="Record URLs visited in Chrome, Firefox, and Edge">
                <Toggle checked={trackUrls} onChange={setTrackUrls} />
              </SettingRow>

              <SettingRow label="Track keyboard activity" description="Detect keyboard usage for idle detection (no keylogging)">
                <Toggle checked={trackKeyboard} onChange={setTrackKeyboard} />
              </SettingRow>

              <SettingRow
                label="Idle timeout"
                description="Mark user as idle after this many minutes of no input"
              >
                <SelectInput
                  value={idleTimeout}
                  onChange={setIdleTimeout}
                  options={[
                    { label: "2 minutes", value: "2" },
                    { label: "5 minutes", value: "5" },
                    { label: "10 minutes", value: "10" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                  ]}
                />
              </SettingRow>
            </div>
          )}

          {/* ── Screenshots ─────────────────────────────────────────── */}
          {tab === "screenshots" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Screenshots</h2>
              <p className="text-sm text-gray-500 mb-5">Configure automatic screenshot capture</p>

              <SettingRow label="Enable screenshots" description="Capture screenshots at regular intervals">
                <Toggle checked={screenshotsEnabled} onChange={setScreenshotsEnabled} />
              </SettingRow>

              <SettingRow
                label="Capture interval"
                description="How often screenshots are taken (when enabled)"
              >
                <SelectInput
                  value={screenshotInterval}
                  onChange={setScreenshotInterval}
                  options={[
                    { label: "Every 5 min", value: "5" },
                    { label: "Every 10 min", value: "10" },
                    { label: "Every 15 min", value: "15" },
                    { label: "Every 30 min", value: "30" },
                    { label: "Every 60 min", value: "60" },
                  ]}
                />
              </SettingRow>

              <SettingRow
                label="Blur screenshots"
                description="Apply a blur effect before uploading for added privacy"
              >
                <Toggle checked={blurScreenshots} onChange={setBlurScreenshots} />
              </SettingRow>

              <SettingRow label="Image quality" description="Higher quality uses more storage">
                <SelectInput
                  value={screenshotQuality}
                  onChange={setScreenshotQuality}
                  options={[
                    { label: "Low (faster)", value: "low" },
                    { label: "Medium", value: "medium" },
                    { label: "High", value: "high" },
                  ]}
                />
              </SettingRow>
            </div>
          )}

          {/* ── Notifications ─────────────────────────────────────────── */}
          {tab === "notifications" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Notifications</h2>
              <p className="text-sm text-gray-500 mb-5">Alerts and automated report delivery</p>

              <SettingRow label="Email alerts" description="Send alerts to the admin email address">
                <Toggle checked={emailAlerts} onChange={setEmailAlerts} />
              </SettingRow>

              <SettingRow
                label="Idle alert threshold"
                description="Send an alert when a device has been idle for this duration"
              >
                <SelectInput
                  value={idleAlertMins}
                  onChange={setIdleAlertMins}
                  options={[
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                    { label: "1 hour", value: "60" },
                    { label: "2 hours", value: "120" },
                    { label: "Never", value: "0" },
                  ]}
                />
              </SettingRow>

              <SettingRow
                label="Offline alert threshold"
                description="Send an alert when a device goes offline for this duration"
              >
                <SelectInput
                  value={offlineAlertMins}
                  onChange={setOfflineAlertMins}
                  options={[
                    { label: "5 minutes", value: "5" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                    { label: "1 hour", value: "60" },
                    { label: "Never", value: "0" },
                  ]}
                />
              </SettingRow>

              <SettingRow label="Daily summary report" description="Receive a daily email with activity highlights">
                <Toggle checked={dailyReport} onChange={setDailyReport} />
              </SettingRow>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
