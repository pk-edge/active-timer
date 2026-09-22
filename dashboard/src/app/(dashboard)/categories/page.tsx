"use client";
import PageLoader from "@/components/PageLoader";
import { useState, useMemo } from "react";
import useSWR from "swr";
import {
  Plus, Trash2, Edit2, Tag, X, Check, ChevronDown, Search,
  Globe, Monitor, Zap, RefreshCw, ChevronRight, ChevronLeft,
} from "lucide-react";
import {
  getTechCategories, createTechCategory, updateTechCategory,
  deleteTechCategory, addCategoryRule, deleteCategoryRule,
  getUncategorized, fmtSeconds,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/AccessDenied";
import ConfirmDialog from "@/components/ConfirmDialog";
import { smartSort } from "@/lib/smartSearch";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import type { AppCategory, CategoriesPage, UncategorizedItem } from "@/types";

const PRESET_COLORS = [
  "#4a0f70", "#22c55e", "#f59e0b", "#a855f7", "#ef4444",
  "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#6366f1",
  "#14b8a6", "#64748b",
];

const PER_PAGE = 6;
const UNCAT_PER_PAGE = 20;

// ── Pagination nav ────────────────────────────────────────────────────────────

function PageNav({
  page, totalPages, total, label,
  onPrev, onNext,
}: {
  page: number; totalPages: number; total: number; label: string;
  onPrev: () => void; onNext: () => void;
}) {
  if (totalPages <= 1) return null;
  return (
    <div className="flex items-center justify-between mt-4 pt-3 border-t border-gray-100">
      <span className="text-xs text-gray-500">
        Page {page} of {totalPages} &nbsp;·&nbsp; {total} {label}
      </span>
      <div className="flex items-center gap-1.5">
        <button
          onClick={onPrev}
          disabled={page <= 1}
          className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          <ChevronLeft size={13} /> Previous
        </button>
        <button
          onClick={onNext}
          disabled={page >= totalPages}
          className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          Next <ChevronRight size={13} />
        </button>
      </div>
    </div>
  );
}

// ── Category form modal ───────────────────────────────────────────────────────

interface CategoryFormProps {
  initial?: AppCategory;
  onClose: () => void;
  onSave: () => void;
}

function CategoryForm({ initial, onClose, onSave }: CategoryFormProps) {
  const [name,  setName]  = useState(initial?.name ?? "");
  const [color, setColor] = useState(initial?.color ?? "#6366f1");
  const [desc,  setDesc]  = useState(initial?.description ?? "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) { setErr("Name is required"); return; }
    setSaving(true);
    try {
      if (initial) {
        await updateTechCategory(initial.id, { name: name.trim(), color, description: desc || undefined });
      } else {
        await createTechCategory({ name: name.trim(), color, description: desc || undefined });
      }
      onSave();
      onClose();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
          <h2 className="text-base font-semibold text-gray-900">
            {initial ? "Edit Category" : "New Category"}
          </h2>
          <button onClick={onClose} className="text-gray-600 hover:text-gray-600"><X size={18} /></button>
        </div>
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Name</label>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Productivity"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-2">Color</label>
            <div className="flex flex-wrap gap-2">
              {PRESET_COLORS.map(c => (
                <button key={c} type="button" onClick={() => setColor(c)}
                        className="w-6 h-6 rounded-full border-2 transition"
                        style={{ background: c, borderColor: color === c ? "#1e293b" : "transparent",
                                 transform: color === c ? "scale(1.15)" : "scale(1)" }} />
              ))}
            </div>
            <div className="flex items-center gap-2 mt-2">
              <input type="color" value={color} onChange={e => setColor(e.target.value)}
                     className="w-8 h-8 rounded cursor-pointer border border-gray-200" />
              <span className="text-xs text-gray-600">Custom</span>
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Description (optional)</label>
            <input value={desc} onChange={e => setDesc(e.target.value)} placeholder="What apps belong here?"
                   className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300" />
          </div>
          {err && <p className="text-xs text-red-500">{err}</p>}
          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose}
                    className="flex-1 border border-gray-200 text-gray-600 rounded-lg py-2 text-sm hover:bg-gray-50 transition">
              Cancel
            </button>
            <button type="submit" disabled={saving}
                    className="flex-1 bg-brand-600 text-white rounded-lg py-2 text-sm font-medium hover:bg-brand-700 transition disabled:opacity-60">
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Quick-assign modal (for uncategorized items) ──────────────────────────────

interface AssignModalProps {
  item: UncategorizedItem;
  categories: AppCategory[];
  onClose: () => void;
  onAssigned: () => void;
}

function AssignModal({ item, categories, onClose, onAssigned }: AssignModalProps) {
  const [catId, setCatId]   = useState(categories[0]?.id ?? "");
  const [saving, setSaving] = useState(false);
  const [err, setErr]       = useState("");

  async function assign() {
    if (!catId) return;
    setSaving(true);
    try {
      await addCategoryRule(catId, item.pattern);
      onAssigned();
      onClose();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-sm mx-4 p-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h2 className="text-base font-semibold text-gray-900">Assign to Category</h2>
          <button onClick={onClose} className="text-gray-600 hover:text-gray-600"><X size={18} /></button>
        </div>

        <div className="flex items-center gap-2 mb-4 bg-gray-50 rounded-lg px-3 py-2">
          {item.kind === "app"
            ? <Monitor size={14} className="text-gray-600 shrink-0" />
            : <Globe size={14} className="text-gray-600 shrink-0" />}
          <span className="text-sm font-medium text-gray-800 truncate">{item.pattern}</span>
          <span className="ml-auto text-xs text-gray-600 shrink-0">
            {item.kind === "app" ? fmtSeconds(item.seconds) : `${item.seconds} visits`}
          </span>
        </div>

        <label className="block text-xs font-medium text-gray-600 mb-1.5">Category</label>
        <div className="relative mb-4">
          <select value={catId} onChange={e => setCatId(e.target.value)}
                  className="w-full appearance-none border border-gray-300 rounded-lg pl-3 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-brand-300">
            {categories.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <ChevronDown size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
        </div>

        {err && <p className="text-xs text-red-500 mb-3">{err}</p>}

        <div className="flex gap-2">
          <button onClick={onClose}
                  className="flex-1 border border-gray-200 text-gray-600 rounded-lg py-2 text-sm hover:bg-gray-50 transition">
            Cancel
          </button>
          <button onClick={assign} disabled={saving || !catId}
                  className="flex-1 bg-brand-600 text-white rounded-lg py-2 text-sm font-medium hover:bg-brand-700 transition disabled:opacity-60">
            {saving ? "Assigning…" : "Assign"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Uncategorized panel ───────────────────────────────────────────────────────

interface UncategorizedPanelProps {
  categories: AppCategory[];
  onAssigned: () => void;
}

function UncategorizedPanel({ categories, onAssigned }: UncategorizedPanelProps) {
  const [days, setDays]           = useState(30);
  const [tab,  setTab]            = useState<"apps" | "urls">("apps");
  const [search, setSearch]       = useState("");
  const [assigning, setAssigning] = useState<UncategorizedItem | null>(null);
  const [expanded, setExpanded]   = useState(false);
  const [ucPage, setUcPage]       = useState(1);

  const since = (() => {
    const d = new Date(); d.setDate(d.getDate() - (days - 1));
    return d.toISOString().slice(0, 10);
  })();
  const until = new Date().toISOString().slice(0, 10);

  const { data, mutate, isLoading } = useSWR(
    expanded ? ["uncategorized", days] : null,
    () => getUncategorized(since, until),
    { revalidateOnFocus: false },
  );

  // Reset page when tab, search or days changes
  const filtered = useMemo(() => {
    setUcPage(1);
    const raw = tab === "apps" ? (data?.apps ?? []) : (data?.urls ?? []);
    if (!search.trim()) return raw;
    return raw.filter(i => i.pattern.toLowerCase().includes(search.toLowerCase()));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, tab, search]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / UNCAT_PER_PAGE));
  const safePage   = Math.min(ucPage, totalPages);
  const pageItems  = filtered.slice((safePage - 1) * UNCAT_PER_PAGE, safePage * UNCAT_PER_PAGE);

  const totalApps = data?.apps.length ?? 0;
  const totalUrls = data?.urls.length ?? 0;
  const total = totalApps + totalUrls;

  return (
    <>
      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden mb-6">
        {/* Panel header */}
        <button
          onClick={() => setExpanded(x => !x)}
          className="w-full flex items-center gap-3 px-5 py-4 hover:bg-gray-50 transition text-left"
        >
          <div className="w-8 h-8 rounded-lg bg-amber-50 flex items-center justify-center shrink-0">
            <Zap size={15} className="text-amber-500" />
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-gray-900">
              Unassigned from Devices
              {total > 0 && (
                <span className="ml-2 text-xs bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded-full font-medium">
                  {total}
                </span>
              )}
            </p>
            <p className="text-xs text-gray-600">
              Apps and websites detected on your devices that aren&apos;t categorized yet
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={e => { e.stopPropagation(); mutate(); }}
              className="p-1.5 text-gray-600 hover:text-gray-700 rounded-lg hover:bg-gray-100 transition"
            >
              <RefreshCw size={13} />
            </button>
            <ChevronDown size={15} className={`text-gray-600 transition-transform ${expanded ? "rotate-180" : ""}`} />
          </div>
        </button>

        {expanded && (
          <div className="border-t border-gray-100">
            {/* Toolbar */}
            <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-100 flex-wrap">
              <div className="flex items-center bg-gray-100 rounded-lg overflow-hidden shrink-0">
                <button
                  onClick={() => setTab("apps")}
                  className={`px-3 py-1 text-xs font-medium flex items-center gap-1.5 transition ${
                    tab === "apps" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"
                  }`}
                >
                  <Monitor size={11} /> Apps
                  {totalApps > 0 && (
                    <span className="bg-gray-200 text-gray-600 text-[10px] px-1.5 rounded-full">{totalApps}</span>
                  )}
                </button>
                <button
                  onClick={() => setTab("urls")}
                  className={`px-3 py-1 text-xs font-medium flex items-center gap-1.5 transition ${
                    tab === "urls" ? "bg-white text-gray-900 shadow-sm" : "text-gray-600"
                  }`}
                >
                  <Globe size={11} /> Websites
                  {totalUrls > 0 && (
                    <span className="bg-gray-200 text-gray-600 text-[10px] px-1.5 rounded-full">{totalUrls}</span>
                  )}
                </button>
              </div>

              <div className="relative flex-1 min-w-[160px]">
                <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600" />
                <input
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  placeholder={`Search ${tab}…`}
                  className="w-full pl-7 pr-3 py-1.5 border border-gray-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-brand-300"
                />
              </div>

              <div className="relative shrink-0">
                <select
                  value={days}
                  onChange={e => setDays(Number(e.target.value))}
                  className="appearance-none bg-white border border-gray-200 rounded-lg pl-3 pr-7 py-1.5 text-xs text-gray-600 focus:outline-none focus:ring-2 focus:ring-brand-300"
                >
                  <option value={7}>Last 7 days</option>
                  <option value={30}>Last 30 days</option>
                  <option value={60}>Last 60 days</option>
                  <option value={90}>Last 90 days</option>
                </select>
                <ChevronDown size={11} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 pointer-events-none" />
              </div>
            </div>

            {/* Item list */}
            {isLoading ? (
              <div className="px-5 py-8 text-center text-sm text-gray-600">Loading…</div>
            ) : filtered.length === 0 ? (
              <div className="px-5 py-10 text-center">
                <div className="w-10 h-10 rounded-full bg-green-50 flex items-center justify-center mx-auto mb-3">
                  <Check size={18} className="text-green-500" />
                </div>
                <p className="text-sm font-medium text-gray-700">
                  {total === 0
                    ? "All detected items are categorized!"
                    : `No ${tab === "apps" ? "apps" : "websites"} to categorize`}
                </p>
                <p className="text-xs text-gray-600 mt-1">Check back after agents report new activity.</p>
              </div>
            ) : (
              <div className="divide-y divide-gray-100">
                {pageItems.map(item => (
                  <div
                    key={item.pattern}
                    className="flex items-center gap-3 px-5 py-3 hover:bg-gray-50 group"
                  >
                    <div className="w-7 h-7 rounded-lg bg-gray-100 flex items-center justify-center shrink-0">
                      {item.kind === "app"
                        ? <Monitor size={13} className="text-gray-600" />
                        : <Globe size={13} className="text-gray-600" />}
                    </div>

                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-gray-800 truncate">{item.pattern}</p>
                      <p className="text-xs text-gray-600">
                        {item.kind === "app"
                          ? fmtSeconds(item.seconds)
                          : `${item.seconds.toLocaleString()} visits`}
                        {" · "}
                        {item.user_count} {item.user_count === 1 ? "user" : "users"}
                      </p>
                    </div>

                    <button
                      onClick={() => setAssigning(item)}
                      className="opacity-0 group-hover:opacity-100 flex items-center gap-1.5 text-xs bg-brand-600 text-white px-3 py-1.5 rounded-lg hover:bg-brand-700 transition shrink-0"
                    >
                      Assign <ChevronRight size={11} />
                    </button>
                  </div>
                ))}

                {/* Uncategorized pagination */}
                <div className="px-5 py-3 flex items-center justify-between border-t border-gray-100 bg-gray-50/50">
                  <span className="text-xs text-gray-500">
                    {filtered.length > 0
                      ? `${(safePage - 1) * UNCAT_PER_PAGE + 1}–${Math.min(safePage * UNCAT_PER_PAGE, filtered.length)} of ${filtered.length}`
                      : "0 items"}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button
                      onClick={() => setUcPage(p => Math.max(1, p - 1))}
                      disabled={safePage <= 1}
                      className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed transition"
                    >
                      <ChevronLeft size={12} /> Prev
                    </button>
                    <span className="text-xs text-gray-500 px-1">{safePage}/{totalPages}</span>
                    <button
                      onClick={() => setUcPage(p => Math.min(totalPages, p + 1))}
                      disabled={safePage >= totalPages}
                      className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed transition"
                    >
                      Next <ChevronRight size={12} />
                    </button>
                  </div>
                </div>
              </div>
            )}

            {filtered.length > 0 && (
              <div className="px-5 py-2 border-t border-gray-100 text-xs text-gray-600 flex items-center gap-1">
                <Zap size={11} className="text-amber-400" />
                Hover a row and click <strong className="text-gray-600 mx-0.5">Assign</strong> to add it to a category instantly
              </div>
            )}
          </div>
        )}
      </div>

      {assigning && (
        <AssignModal
          item={assigning}
          categories={categories}
          onClose={() => setAssigning(null)}
          onAssigned={() => { mutate(); onAssigned(); }}
        />
      )}
    </>
  );
}

// ── Category card ─────────────────────────────────────────────────────────────

function CategoryCard({
  cat, canEdit, onEdit, onDelete, onRefresh,
}: {
  cat: AppCategory;
  canEdit: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [search, setSearch]     = useState("");
  const [newRule, setNewRule]   = useState("");
  const [adding, setAdding]     = useState(false);
  const [addErr, setAddErr]     = useState("");

  const rules = cat.rules.filter(r =>
    !search || r.app_pattern.toLowerCase().includes(search.toLowerCase())
  );

  async function handleAddRule(e: React.FormEvent) {
    e.preventDefault();
    const pattern = newRule.trim();
    if (!pattern) return;
    setAdding(true); setAddErr("");
    try {
      await addCategoryRule(cat.id, pattern);
      setNewRule("");
      onRefresh();
    } catch (e: unknown) {
      setAddErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setAdding(false);
    }
  }

  async function handleDeleteRule(ruleId: string) {
    try { await deleteCategoryRule(ruleId); onRefresh(); } catch { /* ignore */ }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      <div className="flex items-center gap-3 px-5 py-4">
        <div className="w-3 h-3 rounded-full shrink-0" style={{ background: cat.color }} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="font-semibold text-gray-900 text-sm">{cat.name}</p>
            {cat.is_system && (
              <span className="text-[10px] bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded-full font-medium">system</span>
            )}
          </div>
          {cat.description && (
            <p className="text-xs text-gray-600 truncate mt-0.5">{cat.description}</p>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <span className="text-xs text-gray-600 mr-1">{cat.rule_count} rules</span>
          {canEdit && (
            <button onClick={onEdit}
                    className="p-1.5 text-gray-600 hover:text-brand-600 hover:bg-brand-50 rounded-lg transition">
              <Edit2 size={14} />
            </button>
          )}
          {canEdit && !cat.is_system && (
            <button onClick={onDelete}
                    className="p-1.5 text-gray-600 hover:text-red-500 hover:bg-red-50 rounded-lg transition">
              <Trash2 size={14} />
            </button>
          )}
          <button onClick={() => setExpanded(x => !x)}
                  className="p-1.5 text-gray-600 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition ml-1">
            <ChevronDown size={14} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
          </button>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-gray-100 px-5 py-4">
          {cat.rule_count > 4 && (
            <div className="relative mb-3">
              <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600" />
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter apps / URLs…"
                     className="w-full pl-7 pr-3 py-1.5 border border-gray-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-brand-300" />
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {rules.map(rule => (
              <span key={rule.id} className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-full border"
                    style={{ background: `${cat.color}12`, borderColor: `${cat.color}40`, color: cat.color }}>
                {rule.app_pattern}
                {canEdit && (
                  <button onClick={() => handleDeleteRule(rule.id)} className="ml-0.5 opacity-60 hover:opacity-100">
                    <X size={10} />
                  </button>
                )}
              </span>
            ))}
            {rules.length === 0 && cat.rule_count > 0 && (
              <p className="text-xs text-gray-600">No results for &quot;{search}&quot;</p>
            )}
            {cat.rule_count === 0 && (
              <p className="text-xs text-gray-600 py-1">No apps assigned yet.</p>
            )}
          </div>

          {canEdit && (
            <form onSubmit={handleAddRule} className="mt-3 flex gap-2">
              <input value={newRule} onChange={e => { setNewRule(e.target.value); setAddErr(""); }}
                     placeholder="Add app name or website…"
                     className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-brand-300" />
              <button type="submit" disabled={adding || !newRule.trim()}
                      className="flex items-center gap-1 bg-brand-600 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-brand-700 transition disabled:opacity-50">
                <Plus size={12} /> Add
              </button>
            </form>
          )}
          {addErr && <p className="text-xs text-red-500 mt-1">{addErr}</p>}
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function CategoriesPage() {
  const { loading, can } = useUser();
  const [showForm,      setShowForm]      = useState(false);
  const [editing,       setEditing]       = useState<AppCategory | undefined>(undefined);
  const [search,        setSearch]        = useState("");
  const [pendingDelete, setPendingDelete] = useState<AppCategory | null>(null);
  const [deleting,      setDeleting]      = useState(false);

  // Separate page state for system and custom sections
  const [sysPage,  setSysPage]  = useState(1);
  const [custPage, setCustPage] = useState(1);

  const canEdit = can("categories", "edit");
  const debouncedSearch = useDebouncedValue(search, 200);

  // Two independent SWR fetches — system and custom categories, each paginated
  const { data: sysData,  mutate: mutateSys } = useSWR<CategoriesPage>(
    ["tech-categories", "system", sysPage, PER_PAGE],
    () => getTechCategories({ page: sysPage, per_page: PER_PAGE, is_system: true }),
    { keepPreviousData: true },
  );
  const { data: custData, mutate: mutateCust } = useSWR<CategoriesPage>(
    ["tech-categories", "custom", custPage, PER_PAGE],
    () => getTechCategories({ page: custPage, per_page: PER_PAGE, is_system: false }),
    { keepPreviousData: true },
  );

  // Client-side filter/rank of the currently-fetched page against the search
  // box — there's no backend search param for categories, and these pages are
  // small, so this mirrors the client-side filtering used everywhere else in
  // the app (see smartSearch.ts).
  const sysItems = useMemo(
    () => smartSort(debouncedSearch, sysData?.items ?? [], c => [c.name, c.description]),
    [sysData, debouncedSearch],
  );
  const custItems = useMemo(
    () => smartSort(debouncedSearch, custData?.items ?? [], c => [c.name, c.description]),
    [custData, debouncedSearch],
  );

  // All categories (no pagination) — used only for the assign modal in UncategorizedPanel
  const { data: allCats, mutate: mutateAll } = useSWR<CategoriesPage>(
    ["tech-categories", "all"],
    () => getTechCategories({ per_page: 200 }),
  );

  function mutateAll3() { mutateSys(); mutateCust(); mutateAll(); }

  async function confirmCatDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await deleteTechCategory(pendingDelete.id);
      mutateAll3();
      setPendingDelete(null);
    } finally {
      setDeleting(false); }
  }

  if (loading) return <PageLoader />;
  if (!can("categories", "view")) return <AccessDenied label="App Categories" />;

  const sysTotal    = sysData?.total  ?? 0;
  const custTotal   = custData?.total ?? 0;
  const totalRules  = [...(sysData?.items ?? []), ...(custData?.items ?? [])].reduce((s, c) => s + c.rule_count, 0);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto pr-1">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-gray-900">App Categories</h1>
          <p className="text-sm text-gray-600 mt-0.5">
            Classify apps and websites into categories to power Technology Utilization insights
          </p>
        </div>
        {canEdit && (
          <button
            onClick={() => { setEditing(undefined); setShowForm(true); }}
            className="flex items-center gap-2 bg-brand-600 hover:bg-brand-700 text-white text-sm px-4 py-2 rounded-lg shadow-sm transition"
          >
            <Plus size={15} /> New Category
          </button>
        )}
      </div>

      {/* Stats */}
      <div className="flex flex-wrap gap-3 mb-6">
        {[
          { label: "Total Categories", value: sysTotal + custTotal,  color: "bg-brand-50 text-brand-600" },
          { label: "System",           value: sysTotal,              color: "bg-gray-50 text-gray-600" },
          { label: "Custom",           value: custTotal,             color: "bg-purple-50 text-purple-600" },
          { label: "Total Rules",      value: totalRules,            color: "bg-green-50 text-green-600" },
        ].map(s => (
          <div key={s.label} className="bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm flex items-center gap-3">
            <div className={`w-8 h-8 rounded-lg ${s.color} flex items-center justify-center`}>
              <Tag size={14} />
            </div>
            <div>
              <p className="text-xl font-bold text-gray-900 leading-none">{s.value}</p>
              <p className="text-xs text-gray-600 mt-0.5">{s.label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Unassigned panel */}
      <UncategorizedPanel
        categories={allCats?.items ?? []}
        onAssigned={mutateAll3}
      />

      {/* Search */}
      <div className="relative mb-4 max-w-sm">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
        <input
          value={search}
          onChange={e => { setSearch(e.target.value); setSysPage(1); setCustPage(1); }}
          placeholder="Search categories…"
          className="w-full pl-9 pr-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white shadow-sm"
        />
      </div>

      {/* System categories */}
      {sysTotal > 0 && (
        <div className="mb-6 bg-white border border-gray-200 rounded-xl shadow-sm p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">
            System Categories
            <span className="ml-2 font-normal normal-case text-gray-400">({sysTotal})</span>
          </p>
          {sysItems.length === 0 && debouncedSearch.trim() ? (
            <p className="text-sm text-gray-500 py-4 text-center">
              No system categories match &quot;{debouncedSearch}&quot;
            </p>
          ) : sysItems.length === 0 ? (
            // Not a search miss — e.g. a stale page after data changed
            // server-side. Empty query always matches everything, so this
            // can only happen when the current page genuinely has no rows.
            <p className="text-sm text-gray-500 py-4 text-center">No system categories on this page.</p>
          ) : (
            <div className="space-y-3">
              {sysItems.map(cat => (
                <CategoryCard
                  key={cat.id}
                  cat={cat}
                  canEdit={canEdit}
                  onEdit={() => { setEditing(cat); setShowForm(true); }}
                  onDelete={() => setPendingDelete(cat)}
                  onRefresh={mutateAll3}
                />
              ))}
            </div>
          )}
          <PageNav
            page={sysPage}
            totalPages={sysData?.total_pages ?? 1}
            total={sysTotal}
            label="system categories"
            onPrev={() => setSysPage(p => Math.max(1, p - 1))}
            onNext={() => setSysPage(p => p + 1)}
          />
        </div>
      )}

      {/* Custom categories */}
      <div className="mb-6 bg-white border border-gray-200 rounded-xl shadow-sm p-5">
        <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-4">
          Custom Categories
          <span className="ml-2 font-normal normal-case text-gray-400">({custTotal})</span>
        </p>
        {custTotal === 0 ? (
          <div className="py-8 text-center">
            <div className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center mx-auto mb-3">
              <Tag size={17} className="text-gray-400" />
            </div>
            <p className="text-sm text-gray-500">No custom categories yet</p>
            {canEdit && (
              <p className="text-xs text-gray-400 mt-1">
                Click <strong>New Category</strong> to create one
              </p>
            )}
          </div>
        ) : custItems.length === 0 && debouncedSearch.trim() ? (
          <p className="text-sm text-gray-500 py-4 text-center">
            No custom categories match &quot;{debouncedSearch}&quot;
          </p>
        ) : custItems.length === 0 ? (
          // Not a search miss — e.g. a stale page after data changed
          // server-side (custTotal > 0 already ruled out the truly-empty case
          // above). Empty query always matches everything, so this can only
          // happen when the current page genuinely has no rows.
          <p className="text-sm text-gray-500 py-4 text-center">No custom categories on this page.</p>
        ) : (
          <>
            <div className="space-y-3">
              {custItems.map(cat => (
                <CategoryCard
                  key={cat.id}
                  cat={cat}
                  canEdit={canEdit}
                  onEdit={() => { setEditing(cat); setShowForm(true); }}
                  onDelete={() => setPendingDelete(cat)}
                  onRefresh={mutateAll3}
                />
              ))}
            </div>
            <PageNav
              page={custPage}
              totalPages={custData?.total_pages ?? 1}
              total={custTotal}
              label="custom categories"
              onPrev={() => setCustPage(p => Math.max(1, p - 1))}
              onNext={() => setCustPage(p => p + 1)}
            />
          </>
        )}
      </div>

      {showForm && (
        <CategoryForm
          initial={editing}
          onClose={() => { setShowForm(false); setEditing(undefined); }}
          onSave={mutateAll3}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        title={`Delete category "${pendingDelete?.name}"?`}
        message="This category and all its app rules will be removed. Historical analytics that referenced this category will show as Uncategorized."
        confirmLabel="Delete Category"
        loading={deleting}
        onConfirm={confirmCatDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
