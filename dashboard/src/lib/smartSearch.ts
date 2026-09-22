/**
 * Smart search matching for client-side filtering.
 *
 * "Smart" = case- and accent-insensitive, multi-term AND matching. A query is
 * split on whitespace into terms; a record matches when EVERY term appears in
 * at least one of the provided fields. So "agha mac" matches a row whose name is
 * "Agha Khan" and hostname is "Aghas-MacBook" — order-independent, partial, and
 * forgiving of diacritics ("José" matches "jose").
 *
 * This is the client-side half of a deliberately parallel pair: the backend
 * runs the equivalent search over Postgres (pg_trgm + unaccent, the same
 * tokenised-AND-across-columns semantics, ranked exact > prefix >
 * word-boundary > fuzzy). Keep the two in sync conceptually — a query that
 * matches (and how it ranks) here should feel the same as it does server-side.
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
  const haystack = fields.map(f => normalize(f ?? "")).join("  ");
  return terms.every(t => haystack.includes(t));
}

// ── Ranking ──────────────────────────────────────────────────────────────────
//
// Score tiers per term, best to worst. Deliberately mirrors the backend's
// ranking (exact > prefix > word-boundary > fuzzy substring) so client-side
// and server-side search feel consistent even though they're two separate
// implementations (this one is plain string ops; the backend's is pg_trgm
// similarity + unaccent). Tier gaps are large so a single higher-tier term
// always outranks any number of lower-tier ones.
const SCORE_EXACT         = 1000; // field equals the term exactly
const SCORE_PREFIX        = 500;  // field starts with the term
const SCORE_WORD_BOUNDARY = 200;  // term starts right after a non-alnum boundary
const SCORE_SUBSTRING     = 50;   // term appears anywhere else in the field

function hasWordBoundaryMatch(field: string, term: string): boolean {
  let idx = field.indexOf(term);
  while (idx !== -1) {
    const prev = idx === 0 ? "" : field[idx - 1];
    if (!prev || !/[a-z0-9]/i.test(prev)) return true;
    idx = field.indexOf(term, idx + 1);
  }
  return false;
}

function scoreTermInField(term: string, field: string): number {
  if (!term || !field) return 0;
  if (field === term) return SCORE_EXACT;
  if (field.startsWith(term)) return SCORE_PREFIX;
  if (hasWordBoundaryMatch(field, term)) return SCORE_WORD_BOUNDARY;
  if (field.includes(term)) return SCORE_SUBSTRING;
  return 0;
}

/**
 * Relevance score for `fields` against `query` — 0 means no match, higher
 * means more relevant. Multi-token queries sum each token's best per-field
 * score, so a record matching more terms at a better tier ranks higher.
 *
 * INVARIANT: `smartScore(q, ...fields) > 0` is exactly equivalent to
 * `smartMatch(q, ...fields)` — the two must never disagree about *whether*
 * something matches, only about how it ranks. This is enforced by
 * construction (score short-circuits to 0 via the same smartMatch check)
 * rather than by parallel logic, so it can't drift out of sync. Any page
 * that switches from smartMatch to smartScore-based filtering keeps
 * identical match results, just gains ordering.
 */
export function smartScore(query: string, ...fields: (string | null | undefined)[]): number {
  if (!smartMatch(query, ...fields)) return 0;

  const terms = searchTerms(query);
  if (terms.length === 0) return 1; // empty query: everything matches, no ranking signal

  const normFields = fields.map(f => normalize(f ?? ""));
  let total = 0;
  for (const term of terms) {
    let best = 0;
    for (const field of normFields) {
      best = Math.max(best, scoreTermInField(term, field));
    }
    total += best;
  }
  return total;
}

/**
 * Filter `items` to those matching `query` (per `smartMatch`/`smartScore`
 * semantics) and sort them by descending relevance. Ties keep their original
 * relative order (stable sort on index, not just insertion order from
 * `Array.prototype.sort`, which not all engines guarantee for equal keys).
 *
 * `getFields` maps an item to the list of strings to search — e.g.
 * `smartSort(query, apps, a => [a.app_name, a.category_name])`.
 */
export function smartSort<T>(
  query: string,
  items: T[],
  getFields: (item: T) => (string | null | undefined)[],
): T[] {
  return items
    .map((item, index) => ({ item, index, score: smartScore(query, ...getFields(item)) }))
    .filter(entry => entry.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map(entry => entry.item);
}
