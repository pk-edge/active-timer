import type { Group } from "@/types";

export interface GroupTreeNode {
  group: Group;
  children: GroupTreeNode[];
}

/** Build a nested tree from a flat group list (single pass). */
export function buildGroupTree(groups: Group[]): GroupTreeNode[] {
  const byParent = new Map<string | null, Group[]>();
  for (const g of groups) {
    const key = g.parent_id ?? null;
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key)!.push(g);
  }
  for (const list of byParent.values()) {
    list.sort((a, b) => a.name.localeCompare(b.name));
  }

  function nodes(parentId: string | null): GroupTreeNode[] {
    return (byParent.get(parentId) ?? []).map(g => ({
      group: g,
      children: nodes(g.id),
    }));
  }
  return nodes(null);
}

export interface FlatGroupRow {
  group: Group;
  depth: number;
}

/** Depth-first flattening for pickers and indented lists. */
export function flattenGroupTree(
  nodes: GroupTreeNode[],
  depth = 0,
  expanded?: Set<string>,
): FlatGroupRow[] {
  const out: FlatGroupRow[] = [];
  for (const n of nodes) {
    out.push({ group: n.group, depth });
    if (!expanded || expanded.has(n.group.id)) {
      out.push(...flattenGroupTree(n.children, depth + 1, expanded));
    }
  }
  return out;
}

/** Filter tree nodes by name/path; keeps ancestors of matches visible. */
export function filterGroupTree(nodes: GroupTreeNode[], query: string): GroupTreeNode[] {
  const q = query.trim().toLowerCase();
  if (!q) return nodes;

  function walk(node: GroupTreeNode): GroupTreeNode | null {
    const selfMatch =
      node.group.name.toLowerCase().includes(q) ||
      (node.group.path?.toLowerCase().includes(q) ?? false);
    const kids = node.children.map(walk).filter(Boolean) as GroupTreeNode[];
    if (selfMatch || kids.length) return { group: node.group, children: kids };
    return null;
  }
  return nodes.map(walk).filter(Boolean) as GroupTreeNode[];
}

export function groupStats(groups: Group[]) {
  const roots = groups.filter(g => !g.parent_id).length;
  const withChildren = groups.filter(g => g.child_count > 0).length;
  const members = groups.reduce((s, g) => s + g.member_count, 0);
  return { total: groups.length, roots, withChildren, members };
}
