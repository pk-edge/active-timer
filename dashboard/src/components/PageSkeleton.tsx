/**
 * Reusable first-load skeleton for dashboard pages.
 *
 * Shown while a page's PRIMARY data is still being fetched (`isLoading && !data`)
 * so the user sees a loading shape instead of a flash of empty/zero content.
 * On RE-navigation, SWR's `keepPreviousData` serves the cached payload instantly,
 * so this skeleton only appears on a genuine cold load.
 *
 * Variants match the page archetypes:
 *   • "dashboard" — title + stat-card row + two large panels (overview, home, team, tech, reports)
 *   • "table"     — title + filter bar + table rows (lists)
 *   • "list"      — title + stacked rows (settings, roles, agents)
 */

function Bar({ className = "" }: { className?: string }) {
  return <div className={`bg-gray-200 rounded animate-pulse ${className}`} />;
}

function Card() {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm">
      <Bar className="h-7 w-16 mb-2" />
      <Bar className="h-3 w-24" />
    </div>
  );
}

export default function PageSkeleton({
  variant = "dashboard",
  cards = 4,
  rows = 8,
}: {
  variant?: "dashboard" | "table" | "list";
  cards?: number;
  rows?: number;
}) {
  return (
    <div className="flex-1 min-h-0 overflow-hidden" aria-busy="true" aria-label="Loading">
      {/* Header */}
      <div className="mb-6">
        <Bar className="h-7 w-56 mb-2" />
        <Bar className="h-4 w-72" />
      </div>

      {variant === "dashboard" && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            {Array.from({ length: cards }).map((_, i) => <Card key={i} />)}
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {[0, 1].map(i => (
              <div key={i} className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
                <Bar className="h-4 w-40 mb-4" />
                <div className="flex items-center justify-center h-[200px]">
                  <div className="w-40 h-40 rounded-full border-[18px] border-gray-100 animate-pulse" />
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {variant === "table" && (
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
          <div className="h-11 bg-gray-50 border-b border-gray-200" />
          {Array.from({ length: rows }).map((_, i) => (
            <div key={i} className="flex items-center gap-6 px-4 py-3.5 border-b border-gray-100 last:border-0">
              <Bar className="h-4 w-40" />
              <Bar className="h-5 w-16 rounded-full" />
              <Bar className="h-4 w-28" />
              <Bar className="h-4 w-20 ml-auto" />
            </div>
          ))}
        </div>
      )}

      {variant === "list" && (
        <div className="space-y-3">
          {Array.from({ length: rows }).map((_, i) => (
            <div key={i} className="bg-white border border-gray-200 rounded-xl p-4 shadow-sm flex items-center gap-4">
              <Bar className="h-10 w-10 rounded-lg shrink-0" />
              <div className="flex-1">
                <Bar className="h-4 w-1/3 mb-2" />
                <Bar className="h-3 w-1/2" />
              </div>
              <Bar className="h-8 w-20 rounded-lg" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
