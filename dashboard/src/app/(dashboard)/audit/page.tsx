"use client";
import useSWR from "swr";
import { ClipboardList } from "lucide-react";
import { getAudit, type AuditEntry } from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";

const ACTION_COLOR: Record<string, string> = {
  create: "bg-green-50 text-green-700",
  delete: "bg-red-50 text-red-700",
  deactivate: "bg-red-50 text-red-700",
  update: "bg-blue-50 text-blue-700",
  activate: "bg-amber-50 text-amber-700",
  upload: "bg-purple-50 text-purple-700",
};

function actionStyle(action: string) {
  const verb = action.split(".")[1] ?? "";
  return ACTION_COLOR[verb] ?? "bg-gray-100 text-gray-600";
}

export default function AuditPage() {
  const { loading, can } = useUser();
  const { data } = useSWR<AuditEntry[]>("audit", () => getAudit(200), { refreshInterval: 30_000 });

  if (loading) return null;
  if (!can("audit", "view")) return <AccessDenied label="the Audit Log" />;

  return (
    <div className="overflow-y-auto pr-1">
      <div className="flex items-center gap-2 mb-5">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">Audit Log</h1>
          <p className="text-gray-500 text-sm mt-0.5">Administrative actions across the organization</p>
        </div>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
              <th className="text-left px-4 py-3 font-semibold">When</th>
              <th className="text-left px-4 py-3 font-semibold">Actor</th>
              <th className="text-left px-4 py-3 font-semibold">Action</th>
              <th className="text-left px-4 py-3 font-semibold">Target</th>
              <th className="text-left px-4 py-3 font-semibold">Details</th>
            </tr>
          </thead>
          <tbody>
            {data?.map(e => (
              <tr key={e.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">
                  {e.created_at ? new Date(e.created_at).toLocaleString() : "—"}
                </td>
                <td className="px-4 py-3 text-gray-700">{e.actor_name ?? "—"}</td>
                <td className="px-4 py-3">
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${actionStyle(e.action)}`}>{e.action}</span>
                </td>
                <td className="px-4 py-3 text-gray-500">{e.target_type ?? "—"}</td>
                <td className="px-4 py-3 text-xs text-gray-400 max-w-xs truncate">
                  {e.metadata ? JSON.stringify(e.metadata) : "—"}
                </td>
              </tr>
            ))}
            {data?.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400 text-sm">
                <ClipboardList size={28} className="mx-auto mb-2 text-gray-200" />
                No audit entries yet.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
