"use client";
import { useState } from "react";
import useSWR from "swr";
import {
  Plus, Trash2, Edit2, Tag, X, Check, ChevronDown, Search,
} from "lucide-react";
import {
  getTechCategories, createTechCategory, updateTechCategory,
  deleteTechCategory, addCategoryRule, deleteCategoryRule,
} from "@/lib/api";
import { useUser } from "@/hooks/useUser";
import { AccessDenied } from "@/components/charts";
import type { AppCategory } from "@/types";

const PRESET_COLORS = [
  "#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ef4444",
  "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#6366f1",
  "#14b8a6", "#64748b",
];

// ── Color picker ──────────────────────────────────────────────────────────────

function ColorDot({ color, selected, onClick }: { color: string; selected: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-6 h-6 rounded-full border-2 transition"
      style={{
        background: color,
        borderColor: selected ? "#1e293b" : "transparent",
        transform: selected ? "scale(1.15)" : "scale(1)",
      }}
    />
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
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-base font-semibold text-gray-900">
            {initial ? "Edit Category" : "New Category"}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Name</label>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="e.g. Productivity"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-2">Color</label>
            <div className="flex flex-wrap gap-2">
              {PRESET_COLORS.map(c => (
                <ColorDot key={c} color={c} selected={color === c} onClick={() => setColor(c)} />
              ))}
            </div>
            <div className="flex items-center gap-2 mt-2">
              <input
                type="color"
                value={color}
                onChange={e => setColor(e.target.value)}
                className="w-8 h-8 rounded cursor-pointer border border-gray-200"
              />
              <span className="text-xs text-gray-400">Custom color</span>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1.5">Description (optional)</label>
            <input
              value={desc}
              onChange={e => setDesc(e.target.value)}
              placeholder="What apps belong here?"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300"
            />
          </div>

          {err && <p className="text-xs text-red-500">{err}</p>}

          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose}
                    className="flex-1 border border-gray-200 text-gray-600 rounded-lg py-2 text-sm hover:bg-gray-50 transition">
              Cancel
            </button>
            <button type="submit" disabled={saving}
                    className="flex-1 bg-blue-600 text-white rounded-lg py-2 text-sm font-medium hover:bg-blue-700 transition disabled:opacity-60">
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Add rule input ────────────────────────────────────────────────────────────

function AddRuleInput({ catId, onAdded }: { catId: string; onAdded: () => void }) {
  const [val, setVal]     = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr]     = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const pattern = val.trim();
    if (!pattern) return;
    setLoading(true); setErr("");
    try {
      await addCategoryRule(catId, pattern);
      setVal("");
      onAdded();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-3 flex gap-2">
      <input
        value={val}
        onChange={e => { setVal(e.target.value); setErr(""); }}
        placeholder="App name (e.g. Chrome, Slack)"
        className="flex-1 border border-gray-200 rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-300"
      />
      <button
        type="submit"
        disabled={loading || !val.trim()}
        className="flex items-center gap-1 bg-blue-600 text-white text-xs px-3 py-1.5 rounded-lg hover:bg-blue-700 transition disabled:opacity-50"
      >
        <Plus size={12} /> Add
      </button>
      {err && <p className="text-xs text-red-500 self-center">{err}</p>}
    </form>
  );
}

// ── Category card ─────────────────────────────────────────────────────────────

function CategoryCard({
  cat, onEdit, onDelete, onRefresh,
}: {
  cat: AppCategory;
  onEdit: () => void;
  onDelete: () => void;
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [search, setSearch]     = useState("");

  const rules = cat.rules.filter(r =>
    !search || r.app_pattern.toLowerCase().includes(search.toLowerCase())
  );

  async function handleDeleteRule(ruleId: string) {
    try {
      await deleteCategoryRule(ruleId);
      onRefresh();
    } catch { /* ignore */ }
  }

  async function handleDelete() {
    if (!confirm(`Delete category "${cat.name}"? All rules will be removed.`)) return;
    setDeleting(true);
    try { await deleteTechCategory(cat.id); onDelete(); }
    catch { setDeleting(false); }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-4">
        <div className="w-3 h-3 rounded-full shrink-0" style={{ background: cat.color }} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="font-semibold text-gray-900 text-sm">{cat.name}</p>
            {cat.is_system && (
              <span className="text-[10px] bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded-full font-medium">
                system
              </span>
            )}
          </div>
          {cat.description && (
            <p className="text-xs text-gray-400 truncate mt-0.5">{cat.description}</p>
          )}
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <span className="text-xs text-gray-400 mr-2">{cat.rule_count} apps</span>
          <button onClick={onEdit}
                  className="p-1.5 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition">
            <Edit2 size={14} />
          </button>
          {!cat.is_system && (
            <button onClick={handleDelete} disabled={deleting}
                    className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition disabled:opacity-50">
              <Trash2 size={14} />
            </button>
          )}
          <button onClick={() => setExpanded(x => !x)}
                  className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded-lg transition ml-1">
            <ChevronDown size={14} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
          </button>
        </div>
      </div>

      {/* Expanded rules */}
      {expanded && (
        <div className="border-t border-gray-100 px-5 py-4">
          {cat.rule_count > 0 && (
            <div className="relative mb-3">
              <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Filter apps…"
                className="w-full pl-7 pr-3 py-1.5 border border-gray-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-300"
              />
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            {rules.map(rule => (
              <span key={rule.id}
                    className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-full border"
                    style={{ background: `${cat.color}12`, borderColor: `${cat.color}40`, color: cat.color }}>
                {rule.app_pattern}
                <button onClick={() => handleDeleteRule(rule.id)}
                        className="ml-0.5 opacity-60 hover:opacity-100 transition">
                  <X size={10} />
                </button>
              </span>
            ))}
            {rules.length === 0 && cat.rule_count > 0 && (
              <p className="text-xs text-gray-400">No results for &quot;{search}&quot;</p>
            )}
            {cat.rule_count === 0 && (
              <p className="text-xs text-gray-400 py-1">No apps assigned yet. Add one below.</p>
            )}
          </div>

          <AddRuleInput catId={cat.id} onAdded={onRefresh} />
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function CategoriesPage() {
  const { loading, can } = useUser();
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing]   = useState<AppCategory | undefined>(undefined);
  const [search, setSearch]     = useState("");

  const { data: categories, mutate } = useSWR<AppCategory[]>("tech-categories", getTechCategories);

  if (loading) return null;
  if (!can("tech", "view")) return <AccessDenied label="Technology Categories" />;

  const filtered = (categories ?? []).filter(c =>
    !search || c.name.toLowerCase().includes(search.toLowerCase())
  );

  const systemCats = filtered.filter(c => c.is_system);
  const customCats = filtered.filter(c => !c.is_system);

  return (
    <div className="overflow-y-auto pr-1">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">App Categories</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Group applications into categories to get richer insights in Technology Utilization
          </p>
        </div>
        {can("tech", "edit") && (
          <button
            onClick={() => { setEditing(undefined); setShowForm(true); }}
            className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white text-sm px-4 py-2 rounded-lg shadow-sm transition"
          >
            <Plus size={15} /> New Category
          </button>
        )}
      </div>

      {/* Search */}
      <div className="relative mb-6 max-w-sm">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search categories…"
          className="w-full pl-9 pr-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-300 bg-white shadow-sm"
        />
      </div>

      {/* Stats banner */}
      <div className="flex flex-wrap gap-4 mb-6">
        {[
          { label: "Total Categories", value: (categories ?? []).length, icon: Tag, color: "bg-blue-50 text-blue-600" },
          { label: "System", value: systemCats.length, icon: Check, color: "bg-gray-50 text-gray-600" },
          { label: "Custom", value: customCats.length, icon: Plus, color: "bg-purple-50 text-purple-600" },
          {
            label: "Total Rules",
            value: (categories ?? []).reduce((s, c) => s + c.rule_count, 0),
            icon: Tag,
            color: "bg-green-50 text-green-600",
          },
        ].map(stat => (
          <div key={stat.label} className="bg-white border border-gray-200 rounded-xl px-4 py-3 flex items-center gap-3 shadow-sm">
            <div className={`w-8 h-8 rounded-lg ${stat.color} flex items-center justify-center`}>
              <stat.icon size={15} />
            </div>
            <div>
              <p className="text-xl font-bold text-gray-900 leading-none">{stat.value}</p>
              <p className="text-xs text-gray-500 mt-0.5">{stat.label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* System categories */}
      {systemCats.length > 0 && (
        <div className="mb-6">
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-3">
            System Categories
          </p>
          <div className="space-y-3">
            {systemCats.map(cat => (
              <CategoryCard
                key={cat.id}
                cat={cat}
                onEdit={() => { setEditing(cat); setShowForm(true); }}
                onDelete={() => mutate()}
                onRefresh={() => mutate()}
              />
            ))}
          </div>
        </div>
      )}

      {/* Custom categories */}
      {customCats.length > 0 && (
        <div className="mb-6">
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-3">
            Custom Categories
          </p>
          <div className="space-y-3">
            {customCats.map(cat => (
              <CategoryCard
                key={cat.id}
                cat={cat}
                onEdit={() => { setEditing(cat); setShowForm(true); }}
                onDelete={() => mutate()}
                onRefresh={() => mutate()}
              />
            ))}
          </div>
        </div>
      )}

      {(categories ?? []).length === 0 && (
        <div className="bg-white border border-gray-200 rounded-xl p-10 text-center shadow-sm">
          <div className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center mx-auto mb-3">
            <Tag size={20} className="text-gray-400" />
          </div>
          <p className="text-gray-700 font-medium">No categories yet</p>
          <p className="text-gray-400 text-sm mt-1">Create a category to start classifying applications.</p>
        </div>
      )}

      {/* Modal */}
      {showForm && (
        <CategoryForm
          initial={editing}
          onClose={() => { setShowForm(false); setEditing(undefined); }}
          onSave={() => mutate()}
        />
      )}
    </div>
  );
}
