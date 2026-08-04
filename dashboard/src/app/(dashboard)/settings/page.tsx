"use client";
import PageLoader from "@/components/PageLoader";
import PageSkeleton from "@/components/PageSkeleton";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import {
  getCompanyInfo, updateCompanySettings,
  type CompanyInfo, type CompanySettings,
} from "@/lib/api";
import { logEvent } from "@/lib/auditLogger";
import {
  Building2, Activity, Bell, Link2,
  Check, ChevronRight, Copy, Eye, EyeOff,
} from "lucide-react";

type Tab = "general" | "tracking" | "notifications" | "integration";

const TABS: { key: Tab; label: string; icon: React.ReactNode }[] = [
  { key: "general",       label: "General",       icon: <Building2 size={16} /> },
  { key: "tracking",      label: "Tracking",      icon: <Activity  size={16} /> },
  { key: "notifications", label: "Notifications", icon: <Bell      size={16} /> },
  { key: "integration",   label: "Integration",   icon: <Link2     size={16} /> },
];

const DEFAULT_SETTINGS: CompanySettings = {
  timezone: "UTC", work_start: "09:00", work_end: "18:00",
  track_urls: true, track_window_titles: true, track_keyboard: false,
  idle_timeout_mins: 5,
  email_alerts: true, idle_alert_mins: 30,
  offline_alert_enabled: true, offline_alert_mins: 30, daily_report: true,
  max_file_size_mb: 500,
};

function Toggle({ checked, onChange, disabled }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <button
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none ${
        checked ? "bg-brand-600" : "bg-gray-300"
      } ${disabled ? "opacity-60 cursor-not-allowed" : ""}`}
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
        {description && <p className="text-xs text-gray-600 mt-0.5">{description}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function SelectInput({ value, onChange, options, disabled }: {
  value: string; onChange: (v: string) => void;
  options: { label: string; value: string }[]; disabled?: boolean;
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={e => onChange(e.target.value)}
      className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

export default function SettingsPage() {
  const { loading, can } = useUser();
  const [tab, setTab] = useState<Tab>("general");
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [tokenVisible, setTokenVisible] = useState(false);
  const [copied, setCopied] = useState(false);

  const { data: companyInfo, mutate: mutateCompany, isLoading } = useSWR<CompanyInfo>("company-info", getCompanyInfo);
  const [savingHeadless, setSavingHeadless] = useState(false);
  const [inactiveDays, setInactiveDays]     = useState<string>("");
  const [inactiveEnabled, setInactiveEnabled] = useState(false);
  const [savingInactive, setSavingInactive]   = useState(false);

  // General
  const [orgName,   setOrgName]   = useState("");
  // All persisted general/tracking/notification prefs live in one object.
  const [prefs, setPrefs] = useState<CompanySettings>(DEFAULT_SETTINGS);
  const setPref = <K extends keyof CompanySettings>(k: K, v: CompanySettings[K]) => {
    setPrefs(p => ({ ...p, [k]: v }));
    setSaveState("idle");
  };

  // Sync local state when company data loads
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (companyInfo && !hydrated) {
      setOrgName(companyInfo.name);
      setPrefs({ ...DEFAULT_SETTINGS, ...(companyInfo.settings ?? {}) });
      const prev = companyInfo.inactive_device_days;
      setInactiveDays(prev != null ? String(prev) : "30");
      setInactiveEnabled(prev != null);
      setHydrated(true);
    }
  }, [companyInfo, hydrated]);

  async function persist(overrides?: {
    headless?: boolean; days?: number | null; settings?: Partial<CompanySettings>;
  }): Promise<boolean> {
    if (!companyInfo) return false;
    try {
      const updated = await updateCompanySettings({
        agent_headless: overrides?.headless ?? !!companyInfo.agent_headless,
        inactive_device_days:
          overrides?.days !== undefined
            ? overrides.days
            : inactiveEnabled ? (parseInt(inactiveDays) || null) : null,
        name: orgName.trim() || undefined,
        settings: { ...prefs, ...(overrides?.settings ?? {}) },
      });
      mutateCompany(updated, { revalidate: false });
      return true;
    } catch {
      return false; // keep previous value on failure
    }
  }

  async function handleSave() {
    if (saveState === "saving") return;
    setSaveState("saving");
    const ok = await persist();
    setSaveState(ok ? "saved" : "error");
    if (ok) {
      logEvent("settings.save", "settings", undefined, { section: tab });
      setTimeout(() => setSaveState("idle"), 2500);
    }
  }

  async function toggleHeadless(v: boolean) {
    if (savingHeadless) return;
    setSavingHeadless(true);
    try { await persist({ headless: v }); }
    finally { setSavingHeadless(false); }
  }

  async function toggleInactiveEnabled(v: boolean) {
    setInactiveEnabled(v);
    setSavingInactive(true);
    try { await persist({ days: v ? (parseInt(inactiveDays) || 30) : null }); }
    finally { setSavingInactive(false); }
  }

  async function saveInactiveDays() {
    if (!inactiveEnabled) return;
    const days = parseInt(inactiveDays);
    if (!days || days < 1) return;
    setSavingInactive(true);
    try { await persist({ days }); }
    finally { setSavingInactive(false); }
  }

  function copyToken() {
    if (!companyInfo) return;
    navigator.clipboard.writeText(companyInfo.enrollment_token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  if (loading) return <PageLoader />;
  if (!can("settings", "view")) return <AccessDenied label="Settings" />;
  if (isLoading && !companyInfo) return <PageSkeleton variant="list" rows={4} />;

  const canEdit = can("settings", "edit");

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900 mb-1">Settings</h1>
          <p className="text-gray-600 text-sm">Configure tracking and notifications</p>
        </div>
        <button
          onClick={handleSave}
          disabled={!canEdit || saveState === "saving"}
          className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition disabled:opacity-60 ${
            saveState === "saved"
              ? "bg-green-50 text-green-600 border border-green-200"
              : saveState === "error"
                ? "bg-red-50 text-red-600 border border-red-200"
                : "bg-brand-600 text-white hover:bg-brand-700"
          }`}
        >
          {saveState === "saved"  && <><Check size={14} /> Saved</>}
          {saveState === "error"  && "Save failed — retry"}
          {saveState === "saving" && "Saving…"}
          {saveState === "idle"   && "Save changes"}
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
                    ? "bg-brand-50 text-brand-700 font-medium"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                }`}
              >
                <span className="flex items-center gap-2.5">
                  {t.icon}
                  {t.label}
                </span>
                {tab === t.key && <ChevronRight size={13} className="text-brand-400" />}
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
              <p className="text-sm text-gray-600 mb-5">Organization details and working hours</p>

              <SettingRow label="Organization name" description="Displayed in reports and notifications">
                <input
                  type="text"
                  value={orgName}
                  onChange={e => { setOrgName(e.target.value); setSaveState("idle"); }}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 w-52 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
                />
              </SettingRow>

              <SettingRow label="Timezone" description="Used for all time displays and reports">
                <SelectInput
                  value={prefs.timezone}
                  onChange={v => setPref("timezone", v)}
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
                  value={prefs.work_start}
                  onChange={e => setPref("work_start", e.target.value)}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
                />
              </SettingRow>

              <SettingRow label="Work hours end" description="End of the standard working day">
                <input
                  type="time"
                  value={prefs.work_end}
                  onChange={e => setPref("work_end", e.target.value)}
                  className="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400"
                />
              </SettingRow>

              <SettingRow
                label="Maximum shared-file size"
                description="Largest file that can be uploaded on the Shared Files page (1–5120 MB)"
              >
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    min={1}
                    max={5120}
                    value={prefs.max_file_size_mb}
                    onChange={e => setPref("max_file_size_mb", parseInt(e.target.value) || 0)}
                    disabled={!canEdit}
                    className="w-24 bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-800 text-center focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 disabled:opacity-50"
                  />
                  <span className="text-sm text-gray-500">MB</span>
                </div>
              </SettingRow>
            </div>
          )}

          {/* ── Tracking ─────────────────────────────────────────── */}
          {tab === "tracking" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Tracking</h2>
              <p className="text-sm text-gray-600 mb-5">Control what activity data the agent captures</p>

              <SettingRow
                label="Make agents invisible (headless)"
                description="Hide the floating timer AND tray icon on ALL agents in this company. Tracking continues silently; running agents apply it within ~60s. You can override this per device on the device page."
              >
                <Toggle checked={!!companyInfo?.agent_headless} onChange={toggleHeadless} disabled={!canEdit || savingHeadless} />
              </SettingRow>

              <SettingRow label="Track active applications" description="Core tracking signal — always on">
                <Toggle checked={true} onChange={() => {}} disabled />
              </SettingRow>

              <SettingRow label="Track window titles" description="Capture the title bar text of the active window. Agents apply this within ~60s.">
                <Toggle checked={prefs.track_window_titles} onChange={v => setPref("track_window_titles", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow label="Track browser URLs" description="Record URLs visited in Chrome, Firefox, and Edge. Agents apply this within ~60s.">
                <Toggle checked={prefs.track_urls} onChange={v => setPref("track_urls", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow label="Track keyboard activity" description="Detect keyboard usage for idle detection (no keylogging)">
                <Toggle checked={prefs.track_keyboard} onChange={v => setPref("track_keyboard", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow
                label="Idle timeout"
                description="Mark user as idle after this many minutes of no input"
              >
                <SelectInput
                  value={String(prefs.idle_timeout_mins)}
                  onChange={v => setPref("idle_timeout_mins", parseInt(v) || 5)}
                  options={[
                    { label: "2 minutes", value: "2" },
                    { label: "5 minutes", value: "5" },
                    { label: "10 minutes", value: "10" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                  ]}
                />
              </SettingRow>

              <SettingRow
                label="Auto-archive inactive devices"
                description="Automatically archive devices that have not been seen for the specified number of days. Archived devices stop receiving data but can be restored."
              >
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={inactiveEnabled}
                    onChange={toggleInactiveEnabled}
                    disabled={!canEdit}
                  />
                  {inactiveEnabled && (
                    <div className="flex items-center gap-1.5">
                      <input
                        type="number"
                        min={1}
                        max={365}
                        value={inactiveDays}
                        onChange={e => setInactiveDays(e.target.value)}
                        onBlur={saveInactiveDays}
                        disabled={savingInactive || !canEdit}
                        className="w-16 bg-white border border-gray-300 rounded-lg px-2 py-1.5 text-sm text-gray-800 text-center focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 disabled:opacity-50"
                      />
                      <span className="text-sm text-gray-500">days</span>
                    </div>
                  )}
                </div>
              </SettingRow>
            </div>
          )}

          {/* ── Integration ─────────────────────────────────────────── */}
          {tab === "integration" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Integration</h2>
              <p className="text-sm text-gray-600 mb-5">
                Company enrollment token — used to register agent installers with this backend
              </p>

              <div className="border border-gray-200 rounded-xl p-5 space-y-4">
                <div>
                  <p className="text-sm font-medium text-gray-800 mb-1">Enrollment Token</p>
                  <p className="text-xs text-gray-600 mb-3">
                    Embed this token in your agent installer so devices can self-enroll.
                    Keep it secret — anyone with this token can enroll new devices.
                  </p>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 flex items-center gap-0 border border-gray-300 rounded-lg overflow-hidden bg-gray-50">
                      <code className="flex-1 px-3 py-2 text-sm font-mono text-gray-700 truncate select-all">
                        {companyInfo
                          ? tokenVisible
                            ? companyInfo.enrollment_token
                            : "•".repeat(companyInfo.enrollment_token.length)
                          : "Loading…"}
                      </code>
                      <button
                        onClick={() => setTokenVisible(v => !v)}
                        title={tokenVisible ? "Hide token" : "Reveal token"}
                        className="px-3 py-2 border-l border-gray-300 text-gray-600 hover:text-gray-700 hover:bg-gray-100 transition"
                      >
                        {tokenVisible ? <EyeOff size={15} /> : <Eye size={15} />}
                      </button>
                    </div>
                    <button
                      onClick={copyToken}
                      disabled={!companyInfo}
                      className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition ${
                        copied
                          ? "bg-green-50 text-green-600 border-green-200"
                          : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
                      }`}
                    >
                      {copied ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy</>}
                    </button>
                  </div>
                </div>

                <div className="pt-3 border-t border-gray-100">
                  <p className="text-sm font-medium text-gray-800 mb-1">Backend URL</p>
                  <p className="text-xs text-gray-600 mb-2">
                    The API base URL agents use to connect. Pass this alongside the enrollment token when building installers.
                  </p>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 px-3 py-2 text-sm font-mono text-gray-700 bg-gray-50 border border-gray-300 rounded-lg truncate select-all">
                      {typeof window !== "undefined" ? window.location.origin : ""}
                    </code>
                    <button
                      onClick={() => {
                        if (typeof window !== "undefined") {
                          navigator.clipboard.writeText(window.location.origin);
                        }
                      }}
                      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border bg-white text-gray-700 border-gray-300 hover:bg-gray-50 transition"
                    >
                      <Copy size={13} /> Copy
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ── Notifications ─────────────────────────────────────────── */}
          {tab === "notifications" && (
            <div>
              <h2 className="text-base font-semibold text-gray-900 mb-1">Notifications</h2>
              <p className="text-sm text-gray-600 mb-5">Alerts and automated report delivery</p>

              <SettingRow label="Email alerts" description="Send alerts to the admin email address">
                <Toggle checked={prefs.email_alerts} onChange={v => setPref("email_alerts", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow
                label="Idle alert threshold"
                description="Send an alert when a device has been idle for this duration"
              >
                <SelectInput
                  value={String(prefs.idle_alert_mins)}
                  onChange={v => setPref("idle_alert_mins", parseInt(v) || 0)}
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
                label="Alert managers when a talent device goes offline"
                description="Notifies Relation Managers (for their groups) and Super Admins in-app when a talent's device crosses the threshold below."
              >
                <Toggle checked={prefs.offline_alert_enabled} onChange={v => setPref("offline_alert_enabled", v)} disabled={!canEdit} />
              </SettingRow>

              <SettingRow
                label="Offline alert threshold"
                description="How long a talent device must be offline before managers are alerted"
              >
                <SelectInput
                  value={String(prefs.offline_alert_mins)}
                  onChange={v => setPref("offline_alert_mins", parseInt(v) || 0)}
                  disabled={!canEdit || !prefs.offline_alert_enabled}
                  options={[
                    { label: "5 minutes", value: "5" },
                    { label: "15 minutes", value: "15" },
                    { label: "30 minutes", value: "30" },
                    { label: "1 hour", value: "60" },
                    { label: "2 hours", value: "120" },
                    { label: "Never", value: "0" },
                  ]}
                />
              </SettingRow>

              <SettingRow label="Daily summary report" description="Receive a daily email with activity highlights">
                <Toggle checked={prefs.daily_report} onChange={v => setPref("daily_report", v)} disabled={!canEdit} />
              </SettingRow>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
