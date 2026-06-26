import { Lock } from "lucide-react";

/**
 * Tiny "no permission" placeholder.
 *
 * Lives in its OWN module (not charts.tsx) on purpose: ~20 pages render this
 * when a user lacks a permission, but most of them never draw a chart. Keeping
 * it out of charts.tsx means importing it does NOT pull the heavy `recharts`
 * library into those route bundles.
 */
export function AccessDenied({ label = "this page" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center">
      <div className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center mb-3">
        <Lock size={20} className="text-gray-600" />
      </div>
      <p className="text-gray-700 font-medium">No access</p>
      <p className="text-gray-600 text-sm mt-1">You don&apos;t have permission to view {label}.</p>
    </div>
  );
}
