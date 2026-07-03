"use client";
import type { FleetReport, DeviceFullReport } from "@/lib/api";

function fmtSecs(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ── Chart image helpers (canvas → base64 PNG for embedding in PDF) ───────────

function createBarChartImage(
  items: { label: string; value: number; value2?: number }[],
  opts: { title: string; color1?: string; color2?: string; legend1?: string; legend2?: string } = { title: "" },
): string {
  const W = 520, H = 210;
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  const pad = { top: 34, right: 16, bottom: 56, left: 52 };
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);

  ctx.fillStyle = "#1f2937"; ctx.font = "bold 13px sans-serif"; ctx.textAlign = "center";
  ctx.fillText(opts.title, W / 2, 22);

  const hasDual = items.some(d => d.value2 !== undefined);
  const maxVal = Math.max(...items.flatMap(d => [d.value, d.value2 ?? 0]), 1);
  const groupW = cw / items.length;
  const barW = hasDual ? (groupW * 0.68) / 2 : groupW * 0.6;
  const barPad = hasDual ? (groupW - barW * 2) / 2 : (groupW - barW) / 2;
  const c1 = opts.color1 ?? "#3b82f6";
  const c2 = opts.color2 ?? "#10b981";

  for (let i = 0; i <= 4; i++) {
    const y = pad.top + ch - (i / 4) * ch;
    ctx.strokeStyle = "#f0f0f0"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + cw, y); ctx.stroke();
    const val = maxVal * (i / 4);
    ctx.fillStyle = "#9ca3af"; ctx.font = "10px sans-serif"; ctx.textAlign = "right";
    ctx.fillText(`${Math.round(val / 60)}m`, pad.left - 4, y + 4);
  }

  items.forEach((d, i) => {
    const gx = pad.left + i * groupW;
    const h1 = (d.value / maxVal) * ch;
    ctx.fillStyle = c1;
    ctx.fillRect(gx + barPad, pad.top + ch - h1, barW, h1);
    if (hasDual && d.value2 !== undefined) {
      const h2 = (d.value2 / maxVal) * ch;
      ctx.fillStyle = c2;
      ctx.fillRect(gx + barPad + barW, pad.top + ch - h2, barW, h2);
    }
    ctx.fillStyle = "#6b7280"; ctx.font = "9px sans-serif"; ctx.textAlign = "center";
    ctx.save();
    ctx.translate(gx + groupW / 2, pad.top + ch + 13);
    if (d.label.length > 6) ctx.rotate(-Math.PI / 4);
    ctx.fillText(d.label, 0, 0);
    ctx.restore();
  });

  if (hasDual && opts.legend1 && opts.legend2) {
    const ly = H - 8;
    ctx.fillStyle = c1; ctx.fillRect(pad.left, ly - 8, 10, 8);
    ctx.fillStyle = "#374151"; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(opts.legend1, pad.left + 14, ly);
    ctx.fillStyle = c2; ctx.fillRect(pad.left + 75, ly - 8, 10, 8);
    ctx.fillText(opts.legend2, pad.left + 89, ly);
  }

  return canvas.toDataURL("image/png");
}

function createHorizontalBarImage(
  items: { label: string; value: number }[],
  opts: { title: string; color?: string } = { title: "" },
): string {
  const W = 520, H = Math.max(140, 40 + items.length * 28);
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  const pad = { top: 34, right: 60, bottom: 10, left: 148 };
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;
  const rowH = ch / items.length;
  const barH = Math.min(rowH * 0.55, 16);
  const color = opts.color ?? "#6366f1";

  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#1f2937"; ctx.font = "bold 13px sans-serif"; ctx.textAlign = "center";
  ctx.fillText(opts.title, W / 2, 22);

  const maxVal = Math.max(...items.map(d => d.value), 1);

  items.forEach((d, i) => {
    const y = pad.top + i * rowH + (rowH - barH) / 2;
    const bw = (d.value / maxVal) * cw;

    ctx.fillStyle = "#f3f4f6"; ctx.fillRect(pad.left, y, cw, barH);
    ctx.fillStyle = color; ctx.fillRect(pad.left, y, bw, barH);

    ctx.fillStyle = "#374151"; ctx.font = "10px sans-serif"; ctx.textAlign = "right";
    const lbl = d.label.length > 20 ? d.label.slice(0, 19) + "…" : d.label;
    ctx.fillText(lbl, pad.left - 6, y + barH / 2 + 4);

    const valStr = `${Math.round(d.value / 60)}m`;
    ctx.fillStyle = bw > 35 ? "#ffffff" : "#6b7280";
    ctx.font = "9px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(valStr, pad.left + bw + 4, y + barH / 2 + 3);
  });

  return canvas.toDataURL("image/png");
}

function createPieChartImage(
  slices: { label: string; value: number }[],
  opts: { title: string } = { title: "" },
): string {
  const PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6"];
  const W = 340, H = 190;
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d")!;

  ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#1f2937"; ctx.font = "bold 13px sans-serif"; ctx.textAlign = "center";
  ctx.fillText(opts.title, W / 2, 22);

  const cx = 110, cy = H / 2 + 14, r = 68;
  const total = slices.reduce((s, d) => s + d.value, 0);
  let angle = -Math.PI / 2;

  slices.forEach((slice, si) => {
    const sweep = total > 0 ? (slice.value / total) * 2 * Math.PI : 0;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, angle, angle + sweep);
    ctx.closePath();
    ctx.fillStyle = PALETTE[si % PALETTE.length]; ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
    angle += sweep;
  });

  let legendY = 44;
  slices.forEach((slice, si) => {
    const pct = total > 0 ? Math.round((slice.value / total) * 100) : 0;
    ctx.fillStyle = PALETTE[si % PALETTE.length]; ctx.fillRect(220, legendY - 9, 10, 10);
    ctx.fillStyle = "#374151"; ctx.font = "10px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(`${slice.label} (${pct}%)`, 234, legendY);
    legendY += 20;
  });

  return canvas.toDataURL("image/png");
}

// ── Device Full Report — PDF ─────────────────────────────────────────────────

export async function downloadDeviceReportPdf(data: DeviceFullReport): Promise<void> {
  const { jsPDF } = await import("jspdf");
  const { autoTable } = await import("jspdf-autotable");

  const doc = new jsPDF({ orientation: "portrait", unit: "mm", format: "a4" });
  const margin = 14;
  let y = 20;

  doc.setFontSize(18);
  doc.setFont("helvetica", "bold");
  doc.text("Device Activity Report", margin, y);
  y += 8;

  doc.setFontSize(9);
  doc.setFont("helvetica", "normal");
  doc.setTextColor(100, 100, 100);
  doc.text(`${data.device.hostname}  ·  ${data.since} → ${data.until}`, margin, y);
  y += 4;
  if (data.device.employee_name) {
    doc.text(`Employee: ${data.device.employee_name}  ·  Platform: ${data.device.platform}`, margin, y);
    y += 4;
  }
  y += 4;
  doc.setTextColor(0, 0, 0);

  const avgPerDay =
    data.working_days > 0
      ? fmtSecs(Math.round(data.total_active_seconds / data.working_days))
      : "—";

  autoTable(doc, {
    startY: y,
    head: [["Total Active", "Total Idle", "Working Days", "Avg / Day"]],
    body: [[
      fmtSecs(data.total_active_seconds),
      fmtSecs(data.total_idle_seconds),
      String(data.working_days),
      avgPerDay,
    ]],
    styles: { fontSize: 9 },
    headStyles: { fillColor: [59, 130, 246] },
    margin: { left: margin, right: margin },
  });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  y = (doc as any).lastAutoTable.finalY + 8;

  if (data.daily_breakdown.length > 0) {
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Daily Breakdown", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["Date", "Active", "Idle", "Utilization"]],
      body: data.daily_breakdown.map(d => {
        const total = d.active_seconds + d.idle_seconds;
        const pct = total > 0 ? Math.round((d.active_seconds / total) * 100) : 0;
        return [d.date, fmtSecs(d.active_seconds), fmtSecs(d.idle_seconds), `${pct}%`];
      }),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [99, 102, 241] },
      margin: { left: margin, right: margin },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    y = (doc as any).lastAutoTable.finalY + 8;

    if (data.daily_breakdown.length > 1) {
      if (y > 200) { doc.addPage(); y = 20; }
      const img = createBarChartImage(
        data.daily_breakdown.map(d => ({ label: d.date.slice(5), value: d.active_seconds, value2: d.idle_seconds })),
        { title: "Daily Activity", color1: "#6366f1", color2: "#d1d5db", legend1: "Active", legend2: "Idle" },
      );
      doc.addImage(img, "PNG", margin, y, 182, 58);
      y += 64;
    }
  }

  if (data.top_apps.length > 0) {
    if (y > 210) { doc.addPage(); y = 20; }
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Top Applications", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["#", "Application", "Time", "Share"]],
      body: data.top_apps.map((a, i) => [i + 1, a.app_name, fmtSecs(a.seconds), `${a.pct}%`]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [16, 185, 129] },
      margin: { left: margin, right: margin },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    y = (doc as any).lastAutoTable.finalY + 8;

    const topN = data.top_apps.slice(0, 10);
    const chartH = 10 + topN.length * 8;
    if (y + chartH > 270) { doc.addPage(); y = 20; }
    const img = createHorizontalBarImage(
      topN.map(a => ({ label: a.app_name, value: a.seconds })),
      { title: "App Usage", color: "#10b981" },
    );
    const imgH = Math.min(60, 16 + topN.length * 6);
    doc.addImage(img, "PNG", margin, y, 182, imgH);
    y += imgH + 6;
  }

  if (data.top_urls.length > 0) {
    if (y > 230) { doc.addPage(); y = 20; }
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Top Websites", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["#", "Domain", "Visits"]],
      body: data.top_urls.map((u, i) => [i + 1, u.url, u.visits]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [245, 158, 11] },
      margin: { left: margin, right: margin },
    });
  }

  doc.save(`device-report-${data.device.hostname}-${data.since}.pdf`);
}

// ── Device Full Report — Excel ───────────────────────────────────────────────

export async function downloadDeviceReportExcel(data: DeviceFullReport): Promise<void> {
  const XLSX = await import("xlsx");

  const wb = XLSX.utils.book_new();

  XLSX.utils.book_append_sheet(
    wb,
    XLSX.utils.aoa_to_sheet([
      ["Device Activity Report"],
      ["Hostname", data.device.hostname],
      ["Employee", data.device.employee_name ?? "Unassigned"],
      ["Platform", data.device.platform],
      ["Period", `${data.since} to ${data.until}`],
      [],
      ["Total Active", fmtSecs(data.total_active_seconds)],
      ["Total Idle", fmtSecs(data.total_idle_seconds)],
      ["Working Days", data.working_days],
      [
        "Avg / Day",
        data.working_days > 0
          ? fmtSecs(Math.round(data.total_active_seconds / data.working_days))
          : "—",
      ],
    ]),
    "Summary",
  );

  if (data.daily_breakdown.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Date", "Active (s)", "Active", "Idle (s)", "Idle"],
        ...data.daily_breakdown.map(d => [
          d.date,
          d.active_seconds,
          fmtSecs(d.active_seconds),
          d.idle_seconds,
          fmtSecs(d.idle_seconds),
        ]),
      ]),
      "Daily",
    );
  }

  if (data.top_apps.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Rank", "Application", "Seconds", "Time", "Share %"],
        ...data.top_apps.map((a, i) => [i + 1, a.app_name, a.seconds, fmtSecs(a.seconds), a.pct]),
      ]),
      "Top Apps",
    );
  }

  if (data.top_urls.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Rank", "Domain", "Visits"],
        ...data.top_urls.map((u, i) => [i + 1, u.url, u.visits]),
      ]),
      "Top Websites",
    );
  }

  XLSX.writeFile(wb, `device-report-${data.device.hostname}-${data.since}.xlsx`);
}

// ── Fleet Report — PDF ───────────────────────────────────────────────────────

export async function downloadFleetReportPdf(data: FleetReport): Promise<void> {
  const { jsPDF } = await import("jspdf");
  const { autoTable } = await import("jspdf-autotable");

  const doc = new jsPDF({ orientation: "portrait", unit: "mm", format: "a4" });
  const margin = 14;
  let y = 20;

  doc.setFontSize(18);
  doc.setFont("helvetica", "bold");
  doc.text("Fleet Activity Report", margin, y);
  y += 8;

  doc.setFontSize(9);
  doc.setFont("helvetica", "normal");
  doc.setTextColor(100, 100, 100);
  doc.text(`${data.since} → ${data.until}`, margin, y);
  y += 8;
  doc.setTextColor(0, 0, 0);

  autoTable(doc, {
    startY: y,
    head: [["Total Devices", "Online", "Offline", "Archived"]],
    body: [[data.total_devices, data.online_devices, data.offline_devices, data.archived_devices]],
    styles: { fontSize: 9 },
    headStyles: { fillColor: [59, 130, 246] },
    margin: { left: margin, right: margin },
  });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  y = (doc as any).lastAutoTable.finalY + 8;

  if (data.platform_breakdown.length > 0) {
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Platform Breakdown", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["Platform", "Devices"]],
      body: data.platform_breakdown.map(p => [p.platform, p.count]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [99, 102, 241] },
      margin: { left: margin, right: margin },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    y = (doc as any).lastAutoTable.finalY + 8;

    if (data.platform_breakdown.length >= 2) {
      if (y > 210) { doc.addPage(); y = 20; }
      const pieImg = createPieChartImage(
        data.platform_breakdown.map(p => ({ label: p.platform, value: p.count })),
        { title: "Platform Distribution" },
      );
      doc.addImage(pieImg, "PNG", margin + 30, y, 122, 68);
      y += 74;
    }
  }

  if (data.daily_activity.length > 0) {
    if (y > 200) { doc.addPage(); y = 20; }
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Daily Activity", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["Date", "Active", "Idle", "Devices"]],
      body: data.daily_activity.map(d => [
        d.date,
        fmtSecs(d.active_seconds),
        fmtSecs(d.idle_seconds),
        d.device_count,
      ]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [16, 185, 129] },
      margin: { left: margin, right: margin },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    y = (doc as any).lastAutoTable.finalY + 8;

    if (data.daily_activity.length > 1) {
      if (y > 200) { doc.addPage(); y = 20; }
      const barImg = createBarChartImage(
        data.daily_activity.map(d => ({ label: d.date.slice(5), value: d.active_seconds, value2: d.idle_seconds })),
        { title: "Daily Activity", color1: "#3b82f6", color2: "#d1d5db", legend1: "Active", legend2: "Idle" },
      );
      doc.addImage(barImg, "PNG", margin, y, 182, 58);
      y += 64;
    }
  }

  if (data.top_apps.length > 0) {
    if (y > 210) { doc.addPage(); y = 20; }
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Top Applications", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["#", "Application", "Total Time"]],
      body: data.top_apps.map((a, i) => [i + 1, a.app_name, fmtSecs(a.seconds)]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [245, 158, 11] },
      margin: { left: margin, right: margin },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    y = (doc as any).lastAutoTable.finalY + 8;

    const topN = data.top_apps.slice(0, 10);
    const appImgH = Math.min(60, 16 + topN.length * 6);
    if (y + appImgH > 270) { doc.addPage(); y = 20; }
    const appImg = createHorizontalBarImage(
      topN.map(a => ({ label: a.app_name, value: a.seconds })),
      { title: "App Usage", color: "#f59e0b" },
    );
    doc.addImage(appImg, "PNG", margin, y, 182, appImgH);
    y += appImgH + 6;
  }

  if (data.most_active_devices.length > 0) {
    if (y > 220) { doc.addPage(); y = 20; }
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.text("Most Active Devices", margin, y);
    y += 4;
    autoTable(doc, {
      startY: y,
      head: [["#", "Hostname", "Employee", "Active Time"]],
      body: data.most_active_devices.map((d, i) => [
        i + 1,
        d.hostname,
        d.employee_name ?? "—",
        fmtSecs(d.active_seconds),
      ]),
      styles: { fontSize: 8 },
      headStyles: { fillColor: [239, 68, 68] },
      margin: { left: margin, right: margin },
    });
  }

  doc.save(`fleet-report-${data.since}-${data.until}.pdf`);
}

// ── Fleet Report — Excel ─────────────────────────────────────────────────────

export async function downloadFleetReportExcel(data: FleetReport): Promise<void> {
  const XLSX = await import("xlsx");

  const wb = XLSX.utils.book_new();

  XLSX.utils.book_append_sheet(
    wb,
    XLSX.utils.aoa_to_sheet([
      ["Fleet Activity Report"],
      ["Period", `${data.since} to ${data.until}`],
      [],
      ["Total Devices", data.total_devices],
      ["Online", data.online_devices],
      ["Offline", data.offline_devices],
      ["Archived", data.archived_devices],
    ]),
    "Summary",
  );

  if (data.platform_breakdown.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Platform", "Devices"],
        ...data.platform_breakdown.map(p => [p.platform, p.count]),
      ]),
      "Platforms",
    );
  }

  if (data.daily_activity.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Date", "Active (s)", "Active", "Idle (s)", "Idle", "Devices"],
        ...data.daily_activity.map(d => [
          d.date,
          d.active_seconds,
          fmtSecs(d.active_seconds),
          d.idle_seconds,
          fmtSecs(d.idle_seconds),
          d.device_count,
        ]),
      ]),
      "Daily Activity",
    );
  }

  if (data.top_apps.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Rank", "Application", "Seconds", "Time"],
        ...data.top_apps.map((a, i) => [i + 1, a.app_name, a.seconds, fmtSecs(a.seconds)]),
      ]),
      "Top Apps",
    );
  }

  if (data.most_active_devices.length > 0) {
    XLSX.utils.book_append_sheet(
      wb,
      XLSX.utils.aoa_to_sheet([
        ["Rank", "Hostname", "Employee", "Active (s)", "Active Time"],
        ...data.most_active_devices.map((d, i) => [
          i + 1,
          d.hostname,
          d.employee_name ?? "—",
          d.active_seconds,
          fmtSecs(d.active_seconds),
        ]),
      ]),
      "Active Devices",
    );
  }

  XLSX.writeFile(wb, `fleet-report-${data.since}-${data.until}.xlsx`);
}
