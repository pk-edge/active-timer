"use client";
/**
 * Lazy chart layer.
 *
 * Every export here is code-split via `next/dynamic`, so the heavy `recharts`
 * library (~100KB+ gzipped) loads ONLY when a chart actually renders — never on
 * a route that merely imports a chart symbol. Pages keep their existing
 * `import { StatusDonut } from "@/components/charts"` and transparently get the
 * lazy version: the page shell paints instantly, then the chart hydrates.
 *
 * `ssr: false` is safe here — the whole dashboard is client-gated behind the
 * auth splash in useUser, so these never needed server rendering anyway.
 *
 * NOTE: `AccessDenied` is re-exported from its own (recharts-free) module for
 * backwards compatibility. Prefer importing it from "@/components/AccessDenied"
 * directly so non-chart pages don't even touch this module.
 */
import dynamic from "next/dynamic";

export { AccessDenied } from "@/components/AccessDenied";

// Lightweight skeletons shown while the recharts chunk streams in.
const donutFallback = () => (
  <div className="h-[220px] flex items-center justify-center">
    <div className="w-[184px] h-[184px] rounded-full border-[18px] border-gray-100 animate-pulse" />
  </div>
);
const barFallback = (h: number) => () => (
  <div className="animate-pulse flex items-end gap-2 px-2" style={{ height: h }}>
    {[60, 85, 45, 95, 70, 55, 80].map((p, i) => (
      <div key={i} className="flex-1 bg-gray-100 rounded-t" style={{ height: `${p}%` }} />
    ))}
  </div>
);

export const StatusDonut = dynamic(
  () => import("@/components/ChartsImpl").then(m => m.StatusDonut),
  { ssr: false, loading: donutFallback },
);

export const ActivityTrend = dynamic(
  () => import("@/components/ChartsImpl").then(m => m.ActivityTrend),
  { ssr: false, loading: barFallback(180) },
);

export const UsagePie = dynamic(
  () => import("@/components/ChartsImpl").then(m => m.UsagePie),
  { ssr: false, loading: donutFallback },
);
