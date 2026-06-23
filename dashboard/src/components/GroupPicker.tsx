"use client";

import { ChevronDown } from "lucide-react";
import useSWR from "swr";
import { getGroups } from "@/lib/api";
import { buildGroupTree, flattenGroupTree } from "@/lib/groupTree";
import type { Group } from "@/types";

interface GroupPickerProps {
  value: string;
  onChange: (id: string) => void;
  allowEmpty?: boolean;
  emptyLabel?: string;
  excludeId?: string;
  className?: string;
  disabled?: boolean;
}

/** Hierarchical team/group selector — used across analytics filters and forms. */
export default function GroupPicker({
  value,
  onChange,
  allowEmpty = true,
  emptyLabel = "All Teams",
  excludeId,
  className = "",
  disabled = false,
}: GroupPickerProps) {
  const { data: groups } = useSWR<Group[]>("groups", getGroups);
  const tree = buildGroupTree((groups ?? []).filter(g => g.id !== excludeId));
  const rows = flattenGroupTree(tree);

  return (
    <div className={`relative shrink-0 ${className}`}>
      <select
        value={value}
        disabled={disabled}
        onChange={e => onChange(e.target.value)}
        className="appearance-none w-full bg-white border border-gray-200 rounded-lg pl-3 pr-8 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-300 shadow-sm disabled:opacity-60"
      >
        {allowEmpty && <option value="">{emptyLabel}</option>}
        {rows.map(({ group, depth }) => (
          <option key={group.id} value={group.id}>
            {`${"— ".repeat(depth)}${group.name}`}
          </option>
        ))}
      </select>
      <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none" />
    </div>
  );
}

interface GroupCheckboxTreeProps {
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}

/** Checkbox tree for assigning group-scoped RBAC access on the users page. */
export function GroupCheckboxTree({ selected, onChange }: GroupCheckboxTreeProps) {
  const { data: groups } = useSWR<Group[]>("groups-for-users", getGroups);
  const tree = buildGroupTree(groups ?? []);
  const rows = flattenGroupTree(tree);

  if (!groups?.length) {
    return <p className="text-xs text-gray-500 px-1">No groups yet.</p>;
  }

  return (
    <div className="max-h-44 overflow-y-auto border border-gray-200 rounded-lg p-2 space-y-0.5">
      {rows.map(({ group, depth }) => (
        <label
          key={group.id}
          className="flex items-center gap-2 text-sm py-0.5 cursor-pointer hover:bg-gray-50 rounded px-1"
          style={{ paddingLeft: `${depth * 14 + 4}px` }}
        >
          <input
            type="checkbox"
            className="accent-[#4a0f70] shrink-0"
            checked={selected.has(group.id)}
            onChange={() => {
              const n = new Set(selected);
              n.has(group.id) ? n.delete(group.id) : n.add(group.id);
              onChange(n);
            }}
          />
          <span className="truncate text-gray-800">{group.name}</span>
          {group.path && depth === 0 && group.child_count > 0 && (
            <span className="text-[10px] text-gray-400 ml-auto shrink-0">{group.child_count} sub</span>
          )}
        </label>
      ))}
    </div>
  );
}
