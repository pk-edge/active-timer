/**
 * Smart search matching for client-side filtering.
 *
 * "Smart" = case- and accent-insensitive, multi-term AND matching. A query is
 * split on whitespace into terms; a record matches when EVERY term appears in
 * at least one of the provided fields. So "agha mac" matches a row whose name is
 * "Agha Khan" and hostname is "Aghas-MacBook" — order-independent, partial, and
 * forgiving of diacritics ("José" matches "jose").
 */

function normalize(s: string): string {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "") // strip combining accent marks
    .toLowerCase()
    .trim();
}

/** Split a raw query into normalized, non-empty search terms. */
export function searchTerms(query: string): string[] {
  return normalize(query).split(/\s+/).filter(Boolean);
}

/**
 * True if every term in `query` is found in at least one of `fields`.
 * An empty query matches everything.
 */
export function smartMatch(query: string, ...fields: (string | null | undefined)[]): boolean {
  const terms = searchTerms(query);
  if (terms.length === 0) return true;
  const haystack = fields.map(f => normalize(f ?? "")).join("  ");
  return terms.every(t => haystack.includes(t));
}
