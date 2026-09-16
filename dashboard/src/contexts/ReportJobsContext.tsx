"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { getReportJob } from "@/lib/api";
import type { FleetReport, DeviceFullReport, DeviceRosterReport, ReportJob } from "@/lib/api";
import {
  downloadFleetReportPdf,
  downloadFleetReportExcel,
  downloadDeviceReportPdf,
  downloadDeviceReportExcel,
  downloadDeviceRosterExcel,
} from "@/lib/reportDownload";

export type ReportFormat = "pdf" | "xlsx" | "both";
export type ReportType = "fleet" | "device_full" | "device_roster";

export interface JobEntry {
  job_id: string;
  label: string;
  type: ReportType;
  format: ReportFormat;
  status: "pending" | "running" | "done" | "failed";
  progress: number;
  error: string | null;
  /** Cached payload of a finished job, so "Open" re-downloads without regenerating. */
  result: ReportJob["result"];
}

interface JobMeta { format: ReportFormat; type: ReportType }

interface ReportJobsCtx {
  jobs: JobEntry[];
  /** Add a job immediately (before API responds). Use a client-generated tempId. */
  addJob(params: { job_id: string; label: string; type: ReportType; format: ReportFormat }): void;
  /** Swap tempId → realId once the backend responds. Pass result for sync-fallback path. */
  activateJob(tempId: string, realId: string, result?: ReportJob["result"]): void;
  /** Mark a job as failed (e.g. network error during POST). */
  failJob(job_id: string, error: string): void;
  /** Re-download a finished job from its cached result — no server round-trip. */
  reopen(job_id: string): void;
  dismiss(job_id: string): void;
}

const ReportJobsContext = createContext<ReportJobsCtx>({
  jobs: [],
  addJob: () => {},
  activateJob: () => {},
  failJob: () => {},
  reopen: () => {},
  dismiss: () => {},
});

export const useReportJobs = () => useContext(ReportJobsContext);

async function triggerDownload(
  type: ReportType,
  format: ReportFormat,
  result: ReportJob["result"],
): Promise<void> {
  if (!result) return;
  try {
    if (type === "fleet") {
      if (format === "pdf"  || format === "both") await downloadFleetReportPdf(result as FleetReport);
      if (format === "xlsx" || format === "both") await downloadFleetReportExcel(result as FleetReport);
    } else if (type === "device_roster") {
      // Roster is an inventory table — Excel only, no PDF variant.
      await downloadDeviceRosterExcel(result as DeviceRosterReport);
    } else {
      if (format === "pdf"  || format === "both") await downloadDeviceReportPdf(result as DeviceFullReport);
      if (format === "xlsx" || format === "both") await downloadDeviceReportExcel(result as DeviceFullReport);
    }
  } catch (e) {
    console.error("Report download failed:", e);
  }
}

export function ReportJobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<JobEntry[]>([]);
  const metaRef = useRef<Map<string, JobMeta>>(new Map());
  // Mirrors `jobs` so reopen() can read the latest entries without re-creating
  // the callback (and without a state updater doing side effects).
  const jobsRef = useRef<JobEntry[]>(jobs);
  jobsRef.current = jobs;

  const dismiss = useCallback((job_id: string) => {
    setJobs(prev => prev.filter(j => j.job_id !== job_id));
    metaRef.current.delete(job_id);
  }, []);

  // Add immediately (optimistic) — shows in sidebar before API responds
  const addJob = useCallback(({
    job_id, label, type, format,
  }: { job_id: string; label: string; type: ReportType; format: ReportFormat }) => {
    metaRef.current.set(job_id, { format, type });
    setJobs(prev => [
      ...prev.filter(j => j.job_id !== job_id), // dedupe on retry
      { job_id, label, type, format, status: "pending", progress: 0, error: null, result: null },
    ]);
  }, []);

  // Called once the backend POST responds with the real job_id
  const activateJob = useCallback((tempId: string, realId: string, result?: ReportJob["result"]) => {
    const meta = metaRef.current.get(tempId);
    if (!meta) return;

    metaRef.current.delete(tempId);
    metaRef.current.set(realId, meta);

    const alreadyDone = !!result;

    setJobs(prev =>
      prev.map(j =>
        j.job_id !== tempId
          ? j
          : {
              ...j,
              job_id: realId,
              status: alreadyDone ? "done" : "pending",
              progress: alreadyDone ? 100 : 0,
              result: result ?? null,
            },
      ),
    );

    // Finished entries stay in the tracker until dismissed, so the result is
    // still there to re-open.
    if (alreadyDone) void triggerDownload(meta.type, meta.format, result!);
  }, []);

  const failJob = useCallback((job_id: string, error: string) => {
    setJobs(prev =>
      prev.map(j => (j.job_id === job_id ? { ...j, status: "failed", error } : j)),
    );
  }, []);

  const reopen = useCallback((job_id: string) => {
    const job = jobsRef.current.find(j => j.job_id === job_id);
    if (job?.result) void triggerDownload(job.type, job.format, job.result);
  }, []);

  // Poll all pending/running jobs every 2 s
  useEffect(() => {
    const active = jobs.filter(
      j => (j.status === "pending" || j.status === "running") && !j.job_id.startsWith("tmp-"),
    );
    if (active.length === 0) return;

    const timer = setTimeout(async () => {
      for (const j of active) {
        try {
          const updated = await getReportJob(j.job_id);
          setJobs(prev =>
            prev.map(p =>
              p.job_id !== j.job_id
                ? p
                : {
                    ...p,
                    status: updated.status,
                    progress: updated.progress,
                    error: updated.error,
                    result: updated.result ?? p.result,
                  },
            ),
          );
          if (updated.status === "done") {
            const meta = metaRef.current.get(j.job_id);
            if (meta) void triggerDownload(meta.type, meta.format, updated.result);
          }
        } catch {
          // silently retry next cycle
        }
      }
    }, 2000);

    return () => clearTimeout(timer);
  }, [jobs]);

  return (
    <ReportJobsContext.Provider value={{ jobs, addJob, activateJob, failJob, reopen, dismiss }}>
      {children}
    </ReportJobsContext.Provider>
  );
}
