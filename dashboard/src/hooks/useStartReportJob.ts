"use client";
import { useCallback } from "react";
import { createReportJob } from "@/lib/api";
import { useReportJobs } from "@/contexts/ReportJobsContext";
import type { ReportFormat, ReportType } from "@/contexts/ReportJobsContext";

export interface StartReportJobParams {
  type: ReportType;
  format: ReportFormat;
  /** Shown in the Downloads tray; include the range so two exports are distinguishable. */
  label: string;
  deviceId?: string;
  /** Required by the API for device_full and fleet; omit for device_roster. */
  since?: string;
  until?: string;
}

// Module-level so ids stay unique across every caller (and across remounts).
let tempSeq = 0;

/**
 * Starts a report job with the optimistic pattern every report trigger shares:
 * show a pending entry in the Downloads tray immediately, POST in the
 * background, then swap the temp id for the real one (or mark it failed).
 *
 * The temp id carries type, device, range and a sequence number, so repeated
 * exports of the same device with different (or identical) ranges never
 * collide. It must keep the `tmp-` prefix — ReportJobsContext skips polling
 * entries that still have it.
 */
export function useStartReportJob() {
  const { addJob, activateJob, failJob } = useReportJobs();

  return useCallback((p: StartReportJobParams) => {
    const tempId = ["tmp", p.type, p.deviceId ?? "all", p.since ?? "", p.until ?? "", Date.now(), ++tempSeq].join("-");
    addJob({ job_id: tempId, label: p.label, type: p.type, format: p.format });

    createReportJob({ type: p.type, device_id: p.deviceId, since: p.since, until: p.until })
      .then(job => activateJob(tempId, job.job_id, job.status === "done" ? job.result ?? undefined : undefined))
      .catch(e => failJob(tempId, e instanceof Error ? e.message : String(e)));
  }, [addJob, activateJob, failJob]);
}
