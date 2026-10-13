"use client";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { getReportJob, HttpError, RateLimitedError } from "@/lib/api";
import type { FleetReport, DeviceFullReport, DeviceRosterReport, ReportJob } from "@/lib/api";
import {
  downloadFleetReportPdf,
  downloadFleetReportExcel,
  downloadDeviceReportPdf,
  downloadDeviceReportExcel,
  downloadDeviceRosterExcel,
} from "@/lib/reportDownload";

// Job polling cadence: start at the floor, back off gently to the ceiling while a
// job is still running, and never go below 2 s — the server rate-limits GETs.
const POLL_MIN_MS = 2_000;
const POLL_MAX_MS = 5_000;
const POLL_BACKOFF = 1.25;
// Consecutive 5xx / network failures (per job) before we give up on it. At the
// 2-5 s cadence that is roughly a minute of the report service being unreachable.
const POLL_MAX_FAILURES = 10;
const LOST_CONTACT_MESSAGE = "Lost contact with the report service — try again";

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
    failureCountsRef.current.delete(job_id);
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

  // Poll all pending/running jobs: 2 s, backing off to 5 s while they keep
  // running. Terminal statuses (done/failed) drop out of `active`, so polling
  // stops by itself once nothing is in flight. A 429 never fails a job — it just
  // pauses polling for the server's Retry-After.
  const pollDelayRef = useRef(POLL_MIN_MS);
  const pausedUntilRef = useRef(0);
  // Consecutive non-429 poll failures per job id; reset on any successful poll.
  const failureCountsRef = useRef<Map<string, number>>(new Map());
  const activeIdsRef = useRef("");
  // Bumped at the end of every poll cycle so the effect re-arms even when a cycle
  // changed no job state (all requests failed, or we were rate-limited).
  const [pollTick, setPollTick] = useState(0);
  // One poll cycle at a time — a jobs change mid-flight must not start a second
  // overlapping cycle (the end-of-cycle tick re-arms the effect anyway).
  const pollingRef = useRef(false);
  useEffect(() => {
    const active = jobs.filter(
      j => (j.status === "pending" || j.status === "running") && !j.job_id.startsWith("tmp-"),
    );
    if (active.length === 0) {
      pollDelayRef.current = POLL_MIN_MS;
      activeIdsRef.current = "";
      return;
    }

    // A new job joining the set resets the cadence so it is picked up promptly.
    const ids = active.map(j => j.job_id).join(",");
    if (ids !== activeIdsRef.current) {
      activeIdsRef.current = ids;
      pollDelayRef.current = POLL_MIN_MS;
    }

    const delay = Math.max(pollDelayRef.current, pausedUntilRef.current - Date.now());
    const timer = setTimeout(async () => {
      if (pollingRef.current) return;
      pollingRef.current = true;
      pollDelayRef.current = Math.min(POLL_MAX_MS, Math.round(pollDelayRef.current * POLL_BACKOFF));
      try {
        await pollOnce(active);
      } finally {
        pollingRef.current = false;
      }
      setPollTick(t => t + 1);
    }, delay);

    async function pollOnce(active: JobEntry[]) {
      for (const j of active) {
        try {
          const updated = await getReportJob(j.job_id);
          failureCountsRef.current.delete(j.job_id); // any success resets the streak
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
        } catch (e) {
          if (e instanceof RateLimitedError) {
            // Not a job failure: wait out Retry-After, then resume polling.
            pausedUntilRef.current = Date.now() + e.retryAfterSeconds * 1000;
            return;
          }
          if (e instanceof HttpError && (e.status === 404 || e.status === 403)) {
            // The job is gone / not ours — polling it again can never succeed.
            setJobs(prev =>
              prev.map(p => (p.job_id === j.job_id ? { ...p, status: "failed", error: "This report job is no longer available." } : p)),
            );
            failureCountsRef.current.delete(j.job_id);
            continue;
          }
          // 5xx / network error: retry next cycle, but not forever.
          const failures = (failureCountsRef.current.get(j.job_id) ?? 0) + 1;
          if (failures >= POLL_MAX_FAILURES) {
            failureCountsRef.current.delete(j.job_id);
            setJobs(prev =>
              prev.map(p => (p.job_id === j.job_id ? { ...p, status: "failed", error: LOST_CONTACT_MESSAGE } : p)),
            );
          } else {
            failureCountsRef.current.set(j.job_id, failures);
          }
        }
      }
    }

    return () => clearTimeout(timer);
  }, [jobs, pollTick]);

  return (
    <ReportJobsContext.Provider value={{ jobs, addJob, activateJob, failJob, reopen, dismiss }}>
      {children}
    </ReportJobsContext.Provider>
  );
}
