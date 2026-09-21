"""Shared free-text search helper: safe LIKE escaping, multi-token AND/OR
matching, and relevance ranking — used by every endpoint that offers a
`search`/`q` query param (audit log, users list, impersonation picker,
shared files).

Why this exists
----------------
Before this module, four endpoints each hand-rolled `f"%{term}%"` and dropped
it straight into ILIKE. Two bugs fell out of that:

1. **Wildcard injection.** `%` and `_` are LIKE metacharacters. A user typing
   `%` got a pattern of `%%%` — matches every row — and `_` matches any single
   character. Nothing sanitized them. `escape_like()` fixes this once, for
   good, everywhere.
2. **No ranking.** Results came back in whatever order the query happened to
   produce (usually insertion order), so searching "Mohammad" across a
   company with five Mohammads never surfaced the best match first.

This module fixes both, and also folds in accent-insensitivity (`unaccent()`)
and tokenized AND/OR matching so backend search behaves identically to the
dashboard's client-side `smartMatch` (see `dashboard/src/lib/smartSearch.ts`):
split the query on whitespace, require every token to match *some* field
(AND across tokens, OR across fields).

Usage
-----
Callers build one `SearchClause` per search param and splice its `where` /
`order_by` SQL fragments into their own query, merging `params`:

    from app.core.search import build_search

    sc = build_search(search, ["a.action", "e.name", "e.email"], prefix="asearch")
    if sc.where:
        filters.append(sc.where)
        params.update(sc.params)
    ...
    order_by = f"{sc.order_by}, a.created_at DESC" if sc.order_by else "a.created_at DESC"

This works both for raw `sqlalchemy.text()` queries (audit.py, adminFiles.py)
and for ORM `select()` queries (users.py) — in the ORM case, wrap the
fragments in `text()` and call `.params(**sc.params)` on the statement; see
the call sites for the exact pattern.

Column safety
-------------
`columns` are SQL expressions (`"a.action"`, `"a.metadata::text"`, ...)
supplied by the *calling code*, never by a request — they are interpolated
directly, same as any other developer-controlled identifier in this
codebase's raw-SQL endpoints. Every value that originates from the request
(the search string, individual tokens) is always passed as a bound
parameter — never string-formatted into SQL.
"""
import math
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# adminFiles.py's `q` param has capped token count at 8 since it shipped;
# every endpoint now shares that cap so a pathological "a b c d e f g h i j k"
# query can't blow up the WHERE clause into dozens of ANDed OR-groups.
MAX_SEARCH_TOKENS = 8

# Trigram similarity below this is mostly noise — two unrelated short strings
# routinely score 0.05-0.15 by sharing a few common trigrams. 0.3 is also
# Postgres's own default for the `pg_trgm.similarity_threshold` GUC (the
# point at which the `%`/`word_similarity` operators themselves consider two
# strings "similar"), so using it here keeps our notion of "related" aligned
# with the extension's. Below the floor we clamp the score to 0 rather than
# leaving raw similarity noise to reorder the unmatched tail of results.
DEFAULT_SIMILARITY_FLOOR = 0.3

# Postgres's default LIKE escape character; used explicitly (rather than
# relying on the default) so the intent is visible at every call site.
_ESCAPE = "\\"


def escape_like(term: str) -> str:
    """Escape a user-supplied string so it can be embedded in a LIKE/ILIKE
    pattern as a *literal* substring — the metacharacters `%` (any run of
    chars) and `_` (any single char) are neutralized, along with the escape
    character itself so a literal backslash in the input can't re-enable them.

    Order matters: the escape character must be escaped FIRST, or escaping
    `%`/`_` afterwards would double-escape the backslashes we just inserted.

    Every LIKE/ILIKE pattern built from request input in this codebase must
    go through this function. Without it, a search for `%` matches every row
    (wildcard-as-input), and `_` matches one arbitrary character per position.
    """
    return (
        term.replace(_ESCAPE, _ESCAPE * 2)
        .replace("%", _ESCAPE + "%")
        .replace("_", _ESCAPE + "_")
    )


def search_tokens(query: str | None, max_tokens: int = MAX_SEARCH_TOKENS) -> list[str]:
    """Split a raw query into whitespace-separated tokens, capped at
    `max_tokens`. Mirrors the frontend's `searchTerms()` splitting (minus the
    accent/case normalization, which happens in SQL via `unaccent(lower(...))`
    so it stays correct against however the DB collation folds case)."""
    if not query:
        return []
    return query.split()[:max_tokens]


@dataclass(frozen=True)
class SearchClause:
    """The SQL fragments + bound params for one search param.

    `where` is a boolean SQL expression (already parenthesized) to AND into
    a WHERE clause, or "" when the query had no usable tokens (caller should
    skip adding it — ANDing in an empty string is invalid SQL, and an absent
    search term should simply not filter anything, matching the old
    `if search: ...` behaviour).

    `order_by` is a ready-to-use ORDER BY expression that already includes
    its own direction (`... DESC`) — use it as
    `ORDER BY {order_by}, <your tiebreaker>`. It is "" under the same
    condition as `where`; callers fall back to their pre-existing ordering.

    `params` holds every bound value referenced by `where`/`order_by`, keyed
    by the `prefix` passed to `build_search()` so multiple SearchClauses (or
    a SearchClause plus a caller's own params) can coexist in one query
    without name collisions.
    """
    where: str = ""
    order_by: str = ""
    params: dict = field(default_factory=dict)


def build_search(
    query: str | None,
    columns: Sequence[str],
    *,
    prefix: str = "search",
    max_tokens: int = MAX_SEARCH_TOKENS,
    similarity_floor: float = DEFAULT_SIMILARITY_FLOOR,
) -> SearchClause:
    """Build a tokenized, accent-insensitive, ranked search over `columns`.

    Matching (WHERE): the query is split into up to `max_tokens` whitespace
    tokens; a row matches when EVERY token is found as a literal (wildcard-
    escaped) substring of AT LEAST ONE column — AND across tokens, OR across
    columns, case- and accent-folded via `lower(unaccent(...))`. This is the
    same semantics as the dashboard's `smartMatch()`, deliberately: a search
    of "agha mac" should mean the same thing whether it runs client-side or
    server-side.

    Ranking (ORDER BY): scores each row's best-matching column against the
    FULL raw query (not the individual tokens — a whole-string comparison is
    what makes "exact"/"prefix" meaningful) on a 4-tier scale, highest wins:
        1.0        exact match (case/accent-folded)
        0.9        prefix match
        0.7        word-boundary match (query starts a word within the field)
        similarity  otherwise, via pg_trgm's similarity(), floored to 0 below
                    `similarity_floor` (see DEFAULT_SIMILARITY_FLOOR docstring)
    The best score across all `columns` (via GREATEST) is what gets sorted on.

    Returns an empty SearchClause (all falsy) when `query` has no tokens, so
    callers can write `if sc.where: ...` and otherwise keep doing nothing
    (empty search = match/rank nothing = don't touch the query).
    """
    tokens = search_tokens(query, max_tokens)
    if not tokens:
        return SearchClause()

    params: dict = {}

    # ── WHERE: AND across tokens, OR across columns ──────────────────────────
    token_clauses = []
    for i, tok in enumerate(tokens):
        key = f"{prefix}_t{i}"
        params[key] = f"%{escape_like(tok)}%"
        col_ors = " OR ".join(
            f"lower(unaccent({col})) LIKE lower(unaccent(:{key})) ESCAPE '{_ESCAPE}'"
            for col in columns
        )
        token_clauses.append(f"({col_ors})")
    where_sql = " AND ".join(token_clauses)

    # ── ORDER BY: 4-tier relevance score against the whole raw query ────────
    raw_query = query.strip()
    qn_key = f"{prefix}_qn"      # normalized whole query, for exact-match + similarity
    qe_key = f"{prefix}_qe"      # LIKE-escaped whole query, for prefix/word-boundary
    floor_key = f"{prefix}_floor"
    params[qn_key] = raw_query
    params[qe_key] = escape_like(raw_query)
    params[floor_key] = similarity_floor

    per_col_scores = []
    for col in columns:
        norm = f"lower(unaccent({col}))"
        qn = f"lower(unaccent(:{qn_key}))"
        qe = f"lower(unaccent(:{qe_key}))"
        per_col_scores.append(f"""
            CASE
                WHEN {norm} = {qn} THEN 1.0
                WHEN {norm} LIKE {qe} || '%' ESCAPE '{_ESCAPE}' THEN 0.9
                WHEN (' ' || {norm}) LIKE '% ' || {qe} || '%' ESCAPE '{_ESCAPE}' THEN 0.7
                ELSE (CASE WHEN similarity({norm}, {qn}) >= :{floor_key}
                           THEN similarity({norm}, {qn}) ELSE 0 END)
            END
        """.strip())
    order_sql = "GREATEST(" + ", ".join(per_col_scores) + ") DESC"

    return SearchClause(where=where_sql, order_by=order_sql, params=params)


# ── BM25 ranking (audit log only — see module docstring) ────────────────────
#
# Why this is a SEPARATE path from build_search() above: BM25 ranks by whole-
# token frequency + corpus-wide inverse document frequency (with optional
# stemming) — it does not do substring or typo matching, so "mohamad" will
# never find "Mohammad" the way build_search()'s pg_trgm path does. That is
# the right tradeoff for audit-log search (genuinely document-like text: an
# action string, actor role, target, a JSON metadata blob) and the wrong one
# for people/hostname/app/URL search, which must keep its typo tolerance.
# Hence: audit.py uses build_bm25_search(); everywhere else keeps build_search().
#
# ParadeDB/pg_search ships a real BM25 index type, but it is a Postgres
# extension and is NOT on the AWS RDS Postgres allowlist this project runs
# on production, so this is hand-rolled on core Postgres tsvector/GIN
# (migration 035_audit_bm25.sql) instead.

# Standard Okapi BM25 free parameters — not tuned for this dataset, just the
# values the BM25 literature (and Lucene/Elasticsearch/ParadeDB's own
# defaults) treat as sane: k1 controls how quickly repeated occurrences of a
# term saturate (higher = each additional occurrence still helps more before
# flattening out), b controls how strongly document length is normalized
# against (0 = ignore length entirely, 1 = fully proportional).
BM25_K1 = 1.2
BM25_B = 0.75


@dataclass(frozen=True)
class Bm25Clause:
    """The SQL fragments + bound params for one BM25-ranked search param —
    same shape/contract as SearchClause (see its docstring): `where` is a
    boolean expression to AND into the caller's WHERE, `order_by` is a
    ready-to-use `ORDER BY` expression (already `DESC`), both "" when the
    query had no usable tokens so the caller's existing `if sc.where: ...`
    pattern works unchanged. `params` holds every bound value, keyed by the
    `prefix` passed to `build_bm25_search()`.
    """
    where: str = ""
    order_by: str = ""
    params: dict = field(default_factory=dict)


async def build_bm25_search(
    db: AsyncSession,
    query: str | None,
    *,
    table: str,
    tsv_column: str,
    company_id: str,
    prefix: str = "bm25",
    max_tokens: int = MAX_SEARCH_TOKENS,
) -> Bm25Clause:
    """Build a real Okapi BM25 recall filter + ranking expression over a
    generated `tsvector` column, scoped to one company.

        score(D, Q) = Σ_t IDF(t) · f(t,D)·(k1+1) / (f(t,D) + k1·(1-b+b·|D|/avgdl))
        IDF(t)      = ln((N - n(t) + 0.5) / (n(t) + 0.5) + 1)

    where N = total documents in the company, n(t) = documents containing
    term t, f(t,D) = term frequency of t in document D, |D| = D's token
    count, avgdl = the company's average document length.

    `table`/`tsv_column` are SQL identifiers supplied by the *calling code*,
    never by a request — same trust model as `columns` in build_search()
    above (see "Column safety" in the module docstring). Every value that
    originates from the request (the search string, individual terms) is
    always passed as a bound parameter.

    Term extraction: reuses `search_tokens()` (whitespace split, lowercased
    by the DB via `to_tsvector`/`plainto_tsquery`, capped at `max_tokens`)
    and treats each token as one BM25 term. This is an accepted
    simplification versus exactly replicating Postgres's own `simple` lexer
    (which sometimes emits multiple lexemes — or a differently-merged one —
    for a single whitespace-delimited token, e.g. hyphenated/dotted
    compounds); it means BM25 recall/ranking is tokenized slightly
    differently from a hand-built substring match would be, which is exactly
    why this path is reserved for audit's document-like content and not used
    for people/hostname search.

    Cost: this runs ONE extra scoped aggregate query per search request to
    get N, avgdl, and each term's document frequency n(t) — a single scan of
    the company's `table` rows (bounded by the indexed `company_id` filter),
    computed once here rather than once per candidate row. audit_log is a
    real but not huge table (thousands of rows per company, not the tens of
    millions activity_events holds — see CODEBASE_GUIDE.md), so a full-table
    aggregate scoped to one company is an acceptable per-request cost, the
    same tradeoff this codebase already documents for `GET /v1/logs`'s
    `has_more` and the report-cache workers.

    Per-row scoring recomputes each candidate's document length and each
    term's frequency inline (via `unnest()` over the tsvector — see below),
    once per term per row, rather than sharing them across terms via a CTE/
    LATERAL join: simpler to splice into a caller's existing FROM clause,
    and — same table-size reasoning as above — an acceptable amount of
    redundant per-row work for this table. A `WITH ... AS (SELECT id,
    score ...)` CTE joined back by id would be the natural next step if
    audit_log ever grows large enough for this to matter.

    `f(t,D)` (a lexeme's occurrence count within one document's tsvector) has
    no direct scalar accessor in Postgres — `length(tsvector)` returns the
    number of *distinct* lexemes, not total occurrences (verified against a
    live Postgres 16: a tsvector built from "login login login" has
    `length() = 1`, not 3), which would silently break BM25's document-length
    normalization. The exact per-lexeme occurrence count is recovered via
    `unnest(tsvector)` (also verified live: it returns `(lexeme, positions,
    weights)` rows, one per distinct lexeme, and the number of stored
    *positions* for a lexeme is precisely its occurrence count), summing
    `array_length(positions, 1)` across all of a document's lexemes for |D|,
    and filtering to a specific term's lexeme(s) — resolved via
    `to_tsvector('simple', term)` so it matches however that same term would
    have been indexed — for f(t,D).

    Returns an empty Bm25Clause (all falsy) when `query` has no tokens, same
    contract as build_search().
    """
    terms = search_tokens(query, max_tokens)
    if not terms:
        return Bm25Clause()

    # Per-row document length: the TRUE total token count (sum of every
    # lexeme's occurrence count), not length(tsv_column) — see docstring.
    doclen_expr = (
        f"(SELECT COALESCE(SUM(array_length(_u.positions, 1)), 0) "
        f"FROM unnest({tsv_column}) AS _u(lexeme, positions, weights))"
    )

    # ── One scoped aggregate query: N, avgdl, and n(t) for every term, in a
    # single scan (multiple FILTER aggregates) rather than one query per term.
    cid_key = f"{prefix}_cid"
    stats_params: dict = {cid_key: company_id}
    df_selects = []
    for i, term in enumerate(terms):
        key = f"{prefix}_dfterm{i}"
        stats_params[key] = term
        df_selects.append(
            f"count(*) FILTER (WHERE {tsv_column} @@ plainto_tsquery('simple', :{key})) AS df{i}"
        )
    stats_sql = (
        f"SELECT count(*) AS n, COALESCE(AVG({doclen_expr}), 0) AS avgdl, "
        + ", ".join(df_selects)
        + f" FROM {table} WHERE company_id = :{cid_key}"
    )
    stats_row = (await db.execute(text(stats_sql), stats_params)).one()
    n_docs = int(stats_row.n or 0)
    # Guard against a division by zero when the company has zero audit rows
    # (the WHERE below will then match nothing anyway, so the exact fallback
    # value here is never actually used to rank anything).
    avgdl = float(stats_row.avgdl or 0) or 1.0

    # ── WHERE: recall filter. plainto_tsquery ANDs the lexemes it derives
    # from the raw query string — mirrors build_search()'s AND-across-tokens
    # semantics. Safe by construction: plainto_tsquery tokenizes its input as
    # plain terms only and never interprets tsquery operator syntax
    # (`&`/`|`/`!`/parentheses) typed by the user, so unlike the LIKE-based
    # path above, no separate escaping helper is needed here.
    raw_query = (query or "").strip()
    query_key = f"{prefix}_query"
    params: dict = {query_key: raw_query}
    where_sql = f"{tsv_column} @@ plainto_tsquery('simple', :{query_key})"

    # ── ORDER BY: Σ_t IDF(t) · f(t,D)·(k1+1) / (f(t,D) + k1·(1-b+b·|D|/avgdl)).
    # N/avgdl/n(t) computed once above; only IDF(t) (itself a function of the
    # already-fetched N/n(t)) and the term string are bound per term below.
    avgdl_key = f"{prefix}_avgdl"
    params[avgdl_key] = avgdl
    term_scores = []
    for i, term in enumerate(terms):
        df = int(getattr(stats_row, f"df{i}") or 0)
        idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)
        idf_key = f"{prefix}_idf{i}"
        term_key = f"{prefix}_term{i}"
        params[idf_key] = idf
        params[term_key] = term
        freq_expr = (
            f"(SELECT COALESCE(SUM(array_length(_u.positions, 1)), 0) "
            f"FROM unnest({tsv_column}) AS _u(lexeme, positions, weights) "
            f"WHERE _u.lexeme = ANY(ARRAY(SELECT _x.lexeme FROM unnest("
            f"to_tsvector('simple', :{term_key})) AS _x(lexeme, positions, weights))))"
        )
        term_scores.append(
            f"(:{idf_key} * ({freq_expr} * ({BM25_K1} + 1)) / "
            f"({freq_expr} + {BM25_K1} * (1 - {BM25_B} + {BM25_B} * {doclen_expr} / :{avgdl_key})))"
        )
    order_by = "(" + " + ".join(term_scores) + ") DESC"

    return Bm25Clause(where=where_sql, order_by=order_by, params=params)
