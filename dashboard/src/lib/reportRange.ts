import { format, parseISO, subDays } from "date-fns";
import type { DateRange } from "@/components/DateRangePicker";

/**
 * Longest date range a device_full / fleet job may cover (inclusive). Mirrors
 * MAX_REPORT_RANGE_DAYS in the backend: two years, 732 days when the window
 * contains a Feb 29, so the DateRangePicker's "Last 2 Years" preset always fits.
 */
export const MAX_REPORT_RANGE_DAYS = 732;

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Today as the backend sees it — the API rejects an `until` after today in UTC. */
export const utcToday = () => new Date().toISOString().slice(0, 10);

function isIsoDay(s: string): boolean {
  if (!ISO_DAY.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  // Round-trip so "2026-02-31" (which Date.UTC silently rolls over) is rejected.
  return new Date(Date.UTC(y, m - 1, d)).toISOString().slice(0, 10) === s;
}

function dayNumber(s: string): number {
  const [y, m, d] = s.split("-").map(Number);
  return Date.UTC(y, m - 1, d) / 86_400_000;
}

/** Inclusive number of days in the range, or null when either end isn't a valid date. */
export function reportRangeDays(range: DateRange): number | null {
  if (!isIsoDay(range.since) || !isIsoDay(range.until)) return null;
  return dayNumber(range.until) - dayNumber(range.since) + 1;
}

/**
 * Client-side twin of the backend's range validation for device_full/fleet
 * jobs. Returns a human-readable reason, or null when the range is valid.
 */
export function validateReportRange(range: DateRange): string | null {
  const days = reportRangeDays(range);
  if (days === null) return "Choose both a start and an end date.";
  if (days < 1) return "Start date must be on or before the end date.";
  const today = utcToday();
  if (range.until > today) return `End date can't be after today (${today} UTC).`;
  if (days > MAX_REPORT_RANGE_DAYS) {
    return `That range is ${days} days; the maximum is ${MAX_REPORT_RANGE_DAYS} (about 2 years).`;
  }
  return null;
}

/**
 * The range the server will actually report on. The DateRangePicker builds
 * ranges from the LOCAL date, but the API buckets days in UTC and rejects an
 * `until` after today in UTC — so for part of the day in UTC+ zones every
 * "ending today" preset is one day ahead. Such a range is shifted back whole
 * (its length kept, so "Last 7 Days" stays seven days) to end on the UTC date.
 * The Reports page queries and its export both use this, which is what keeps
 * their numbers identical. Unparseable ranges (a half-typed custom date) are
 * returned as-is so validateReportRange can report them.
 */
export function normalizeReportRange(range: DateRange): DateRange {
  if (!isIsoDay(range.since) || !isIsoDay(range.until)) return range;
  const today = utcToday();
  if (range.until <= today) return range;
  const shift = dayNumber(range.until) - dayNumber(today);
  const since = format(subDays(parseISO(range.since), shift), "yyyy-MM-dd");
  return { since, until: today };
}

/**
 * "Last 30 Days", ending today. `until` is held to the UTC date so the default
 * stays valid in the hours when the local date is already ahead of UTC.
 */
export function defaultReportRange(): DateRange {
  const local = format(new Date(), "yyyy-MM-dd");
  const until = local > utcToday() ? utcToday() : local;
  return { since: format(subDays(parseISO(until), 29), "yyyy-MM-dd"), until };
}

/** "2026-09-01 → 2026-09-30" — used in job labels so each export is identifiable. */
export function describeReportRange(range: DateRange): string {
  return `${range.since} → ${range.until}`;
}
