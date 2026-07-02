/**
 * ReportGenerator — produces self-contained HTML reports that open in a new
 * browser tab and can be saved as PDF via Ctrl+P / "Save as PDF".
 *
 * No external dependencies required — uses inline SVG for charts.
 */

import type { FleetReport, DeviceFullReport } from "@/lib/api";

// ── Shared helpers ────────────────────────────────────────────────────────────

function fmtSecs(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtDate(d: string): string {
  return new Date(d + "T00:00:00").toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric",
  });
}

// ── SVG bar-chart generator ───────────────────────────────────────────────────

interface BarData { label: string; value: number; color?: string }

function barChart(
  data: BarData[],
  { width = 640, height = 180, barColor = "#4f46e5", labelKey = "label" } = {},
): string {
  if (data.length === 0) return "<p style='color:#9ca3af;font-size:13px'>No data</p>";
  const max = Math.max(...data.map(d => d.value), 1);
  const barW = Math.floor((width - 40) / data.length) - 4;
  const chartH = height - 40;

  const bars = data.map((d, i) => {
    const bh = Math.max(2, Math.round((d.value / max) * chartH));
    const x = 20 + i * (barW + 4);
    const y = chartH - bh + 10;
    const color = d.color ?? barColor;
    const lbl = d.label.length > 8 ? d.label.slice(0, 7) + "…" : d.label;
    return `
      <rect x="${x}" y="${y}" width="${barW}" height="${bh}" rx="3" fill="${color}" opacity="0.85"/>
      <text x="${x + barW / 2}" y="${chartH + 26}" text-anchor="middle" font-size="9" fill="#6b7280">${lbl}</text>`;
  }).join("");

  return `<svg width="${width}" height="${height}" style="overflow:visible">
    <line x1="20" y1="10" x2="20" y2="${chartH + 10}" stroke="#e5e7eb" stroke-width="1"/>
    <line x1="20" y1="${chartH + 10}" x2="${width - 20}" y2="${chartH + 10}" stroke="#e5e7eb" stroke-width="1"/>
    ${bars}
  </svg>`;
}

// ── Horizontal bar chart (for top apps / urls) ────────────────────────────────

function hbarChart(
  rows: { label: string; value: number; pct?: number }[],
  barColor = "#4f46e5",
): string {
  const top = rows.slice(0, 12);
  if (top.length === 0) return "<p style='color:#9ca3af;font-size:13px'>No data</p>";
  const max = Math.max(...top.map(r => r.value), 1);

  const lines = top.map(r => {
    const w = Math.round((r.value / max) * 280);
    const label = r.label.length > 28 ? r.label.slice(0, 27) + "…" : r.label;
    const valStr = typeof r.pct === "number" ? `${r.pct}%` : fmtSecs(r.value);
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="width:180px;font-size:12px;color:#374151;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${label}</span>
      <div style="width:${w}px;height:10px;background:${barColor};border-radius:3px;opacity:0.8;flex-shrink:0"></div>
      <span style="font-size:12px;color:#6b7280;white-space:nowrap">${valStr}</span>
    </div>`;
  }).join("");

  return `<div style="padding:4px 0">${lines}</div>`;
}

// ── Donut chart for platform breakdown ───────────────────────────────────────

const PLATFORM_COLORS: Record<string, string> = {
  Windows: "#3b82f6",
  Macos:   "#8b5cf6",
  Linux:   "#10b981",
  Other:   "#f59e0b",
};

function donutChart(slices: { label: string; value: number }[]): string {
  const total = slices.reduce((s, r) => s + r.value, 0) || 1;
  let angle = -90;
  const cx = 70, cy = 70, r = 55, ir = 32;
  let paths = "";
  const legend: string[] = [];

  slices.forEach((sl, i) => {
    const sweep = (sl.value / total) * 360;
    const color = PLATFORM_COLORS[sl.label] ?? "#94a3b8";
    const start = angle;
    angle += sweep;
    const a1 = (start * Math.PI) / 180;
    const a2 = (angle * Math.PI) / 180;
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    const x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
    const ix1 = cx + ir * Math.cos(a1), iy1 = cy + ir * Math.sin(a1);
    const ix2 = cx + ir * Math.cos(a2), iy2 = cy + ir * Math.sin(a2);
    const large = sweep > 180 ? 1 : 0;
    paths += `<path d="M${ix1},${iy1} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} L${ix2},${iy2} A${ir},${ir} 0 ${large} 0 ${ix1},${iy1} Z" fill="${color}" opacity="0.85"/>`;
    legend.push(`<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
      <div style="width:10px;height:10px;border-radius:2px;background:${color};flex-shrink:0"></div>
      <span style="font-size:12px;color:#374151">${sl.label} <span style="color:#9ca3af">(${sl.value})</span></span>
    </div>`);
  });

  return `<div style="display:flex;align-items:center;gap:24px">
    <svg width="140" height="140"><circle cx="${cx}" cy="${cy}" r="${ir - 1}" fill="white"/>${paths}</svg>
    <div>${legend.join("")}</div>
  </div>`;
}

// ── Shared HTML shell ─────────────────────────────────────────────────────────

function htmlShell(title: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>${title}</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;color:#111827;font-size:14px;line-height:1.5}
    .page{max-width:900px;margin:0 auto;padding:32px 24px}
    .header{border-bottom:3px solid #4f46e5;padding-bottom:16px;margin-bottom:28px}
    .header h1{font-size:22px;font-weight:700;color:#1e1b4b}
    .header .sub{font-size:13px;color:#6b7280;margin-top:4px}
    .logo{font-size:18px;font-weight:800;color:#4f46e5;margin-bottom:8px}
    .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:28px}
    .stat{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;text-align:center}
    .stat .val{font-size:26px;font-weight:700;color:#1e1b4b}
    .stat .lbl{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
    .stat.green .val{color:#059669} .stat.red .val{color:#dc2626}
    .stat.amber .val{color:#d97706} .stat.blue .val{color:#2563eb}
    .section{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:20px}
    .section h2{font-size:14px;font-weight:600;color:#374151;text-transform:uppercase;letter-spacing:.05em;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #f3f4f6}
    .kv{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
    .kv span{font-size:12px;color:#6b7280} .kv strong{font-size:12px;color:#111827}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th{background:#f9fafb;color:#6b7280;text-transform:uppercase;font-size:10px;letter-spacing:.05em;padding:8px 10px;text-align:left;border-bottom:1px solid #e5e7eb}
    td{padding:7px 10px;border-bottom:1px solid #f3f4f6;color:#374151}
    tr:last-child td{border-bottom:none}
    .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:600}
    .badge.green{background:#dcfce7;color:#15803d} .badge.gray{background:#f3f4f6;color:#6b7280}
    .badge.amber{background:#fef3c7;color:#92400e} .badge.blue{background:#dbeafe;color:#1d4ed8}
    .footer{text-align:center;font-size:11px;color:#9ca3af;margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb}
    @media print{body{background:white}.page{padding:16px 0}@page{margin:12mm 10mm}}
  </style>
</head>
<body>
  <div class="page">
    ${body}
    <div class="footer">Generated by TimeTracker &mdash; ${new Date().toLocaleString()} &nbsp;&bull;&nbsp; Use <kbd>Ctrl+P</kbd> / <kbd>⌘P</kbd> to save as PDF</div>
  </div>
</body>
</html>`;
}

// ── Safe report opener — Blob URL avoids document.write() XSS risk ───────────

function openReport(html: string): void {
  const blob = new Blob([html], { type: "text/html" });
  const url  = URL.createObjectURL(blob);
  const tab  = window.open(url, "_blank");
  // Revoke the object URL once the tab has loaded to free memory.
  if (tab) {
    tab.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
  } else {
    // Fallback: revoke after a short delay if the tab reference was blocked.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

// ── Fleet Report ──────────────────────────────────────────────────────────────

export function openFleetReport(data: FleetReport): void {
  const totalHours = Math.round(
    data.daily_activity.reduce((s, d) => s + d.active_seconds, 0) / 3600,
  );
  const avgDailyDevices =
    data.daily_activity.length > 0
      ? Math.round(data.daily_activity.reduce((s, d) => s + d.device_count, 0) / data.daily_activity.length)
      : 0;

  const activityChartData = data.daily_activity.map(d => ({
    label: d.date.slice(5), // MM-DD
    value: Math.round(d.active_seconds / 3600),
  }));

  const body = `
    <div class="header">
      <div class="logo">TimeTracker</div>
      <h1>Fleet Activity Report</h1>
      <div class="sub">Period: ${fmtDate(data.since)} &ndash; ${fmtDate(data.until)}</div>
    </div>

    <div class="stats">
      <div class="stat"><div class="val">${data.total_devices}</div><div class="lbl">Total Devices</div></div>
      <div class="stat green"><div class="val">${data.online_devices}</div><div class="lbl">Online Now</div></div>
      <div class="stat gray"><div class="val">${data.offline_devices}</div><div class="lbl">Offline</div></div>
      <div class="stat amber"><div class="val">${data.archived_devices}</div><div class="lbl">Archived</div></div>
      <div class="stat blue"><div class="val">${totalHours}h</div><div class="lbl">Total Active Hours</div></div>
      <div class="stat"><div class="val">${avgDailyDevices}</div><div class="lbl">Avg Daily Devices</div></div>
    </div>

    <div class="section">
      <h2>Daily Activity (Active Hours)</h2>
      ${barChart(activityChartData, { width: 820, height: 180, barColor: "#4f46e5" })}
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="section">
        <h2>Platform Breakdown</h2>
        ${donutChart(data.platform_breakdown.map(p => ({ label: p.platform, value: p.count })))}
      </div>
      <div class="section">
        <h2>Top Applications</h2>
        ${hbarChart(data.top_apps.map(a => ({ label: a.app_name, value: a.seconds })), "#4f46e5")}
      </div>
    </div>

    <div class="section">
      <h2>Most Active Devices</h2>
      <table>
        <thead><tr><th>#</th><th>Hostname</th><th>Employee</th><th>Active Time</th></tr></thead>
        <tbody>
          ${data.most_active_devices.map((d, i) => `
            <tr>
              <td>${i + 1}</td>
              <td><strong>${d.hostname}</strong></td>
              <td>${d.employee_name ?? '<span style="color:#9ca3af">—</span>'}</td>
              <td>${fmtSecs(d.active_seconds)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  openReport(htmlShell(`Fleet Report ${data.since} – ${data.until}`, body));
}

// ── Device Full Report ────────────────────────────────────────────────────────

export function openDeviceReport(data: DeviceFullReport): void {
  const d = data.device;
  const dailyChartData = data.daily_breakdown.map(row => ({
    label: row.date.slice(5),
    value: Math.round(row.active_seconds / 3600),
  }));

  const body = `
    <div class="header">
      <div class="logo">TimeTracker</div>
      <h1>${d.hostname} &mdash; Activity Report</h1>
      <div class="sub">
        ${fmtDate(data.since)} &ndash; ${fmtDate(data.until)} &nbsp;&bull;&nbsp;
        ${d.platform} &nbsp;&bull;&nbsp;
        ${d.employee_name ? `Assigned to ${d.employee_name}` : "Unassigned"}
      </div>
    </div>

    <div class="stats">
      <div class="stat blue"><div class="val">${fmtSecs(data.total_active_seconds)}</div><div class="lbl">Total Active</div></div>
      <div class="stat gray"><div class="val">${fmtSecs(data.total_idle_seconds)}</div><div class="lbl">Total Idle</div></div>
      <div class="stat green"><div class="val">${data.working_days}</div><div class="lbl">Working Days</div></div>
      <div class="stat"><div class="val">${data.working_days > 0 ? fmtSecs(Math.round(data.total_active_seconds / data.working_days)) : "—"}</div><div class="lbl">Avg / Day</div></div>
    </div>

    <div class="section">
      <h2>Daily Active Hours</h2>
      ${barChart(dailyChartData, { width: 820, height: 180, barColor: "#4f46e5" })}
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="section">
        <h2>Top Applications</h2>
        ${hbarChart(data.top_apps.map(a => ({ label: a.app_name, value: a.seconds, pct: a.pct })), "#4f46e5")}
      </div>
      <div class="section">
        <h2>Top Websites</h2>
        ${hbarChart(data.top_urls.map(u => ({ label: u.url, value: u.visits })), "#0ea5e9")}
      </div>
    </div>

    <div class="section">
      <h2>Day-by-Day Breakdown</h2>
      <table>
        <thead><tr><th>Date</th><th>Active Time</th><th>Idle Time</th><th>Utilisation</th></tr></thead>
        <tbody>
          ${data.daily_breakdown.map(row => {
            const total = row.active_seconds + row.idle_seconds || 1;
            const util = Math.round((row.active_seconds / total) * 100);
            const badge = util >= 70 ? "green" : util >= 40 ? "blue" : "gray";
            return `<tr>
              <td>${fmtDate(row.date)}</td>
              <td>${fmtSecs(row.active_seconds)}</td>
              <td>${fmtSecs(row.idle_seconds)}</td>
              <td><span class="badge ${badge}">${util}%</span></td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>

    <div class="section">
      <h2>Device Information</h2>
      <div class="kv"><span>Hostname:</span><strong>${d.hostname}</strong></div>
      <div class="kv"><span>Platform:</span><strong>${d.platform}</strong></div>
      <div class="kv"><span>Employee:</span><strong>${d.employee_name ?? "Unassigned"}</strong></div>
      <div class="kv"><span>Enrolled:</span><strong>${d.enrolled_at}</strong></div>
      <div class="kv"><span>Last Seen:</span><strong>${d.last_seen ?? "—"}</strong></div>
    </div>`;

  openReport(htmlShell(`${d.hostname} — ${data.since} to ${data.until}`, body));
}
