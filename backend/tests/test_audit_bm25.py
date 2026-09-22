"""GET /v1/audit `search` — hand-rolled Okapi BM25 ranking (migration
035_audit_bm25.sql, app/core/search.py::build_bm25_search()).

DB-touching tests run against the live Postgres in a never-committed session
(same pattern as test_search.py), tagged with uuid.uuid4().hex[:8] so fixture
rows never collide across runs.

The vocabulary used in every fixture below is deliberately plain lowercase
ASCII words with no punctuation (no dots/hyphens/emails) — Postgres's default
text-search parser special-cases compound-looking tokens (hostnames, dotted
names, hyphenated words) and can tokenize them very differently from a naive
split (verified empirically: `to_tsvector('simple', 'user.create-ab12cd34')`
does NOT contain a standalone 'create' lexeme). Keeping the vocabulary plain
means the independent Python reference tokenizer below is provably identical
to Postgres's for these fixtures, rather than a guess.
"""
import math
import re
import uuid

import pytest
from sqlalchemy import text

from app.core.security import create_admin_token
from app.db.models import AuditLog, Company, Employee, Role, RolePermission


# ── Independent BM25 reference (NOT importing app.core.search) ──────────────
#
# Written fresh here, from the same published formula, so a bug shared with
# the production implementation wouldn't silently cancel out in these tests.

BM25_K1 = 1.2
BM25_B = 0.75


def _tokenize(s: str) -> list[str]:
    """Reference tokenizer for the plain lowercase-alnum vocabulary used in
    these fixtures only — split on runs of non-alphanumeric characters,
    lowercase. See module docstring: this is NOT a general reimplementation
    of Postgres's parser, just an exact match for this deliberately simple
    vocabulary."""
    return re.findall(r"[a-z0-9]+", s.lower())


def _row_tokens(action: str, actor_role: str, target_type: str, target_id: str, metadata: dict | None) -> list[str]:
    """Mirrors the generated column's concatenation:
    action || ' ' || actor_role || ' ' || target_type || ' ' || target_id || ' ' || metadata::text
    """
    parts = [action or "", actor_role or "", target_type or "", target_id or ""]
    if metadata:
        for k, v in metadata.items():
            parts.append(str(k))
            parts.append(str(v))
    return _tokenize(" ".join(parts))


def _bm25_reference(docs: dict[str, list[str]], query_terms: list[str]) -> dict[str, float]:
    """docs: {row_key: token_list} for EVERY document in the scope (company),
    matching what build_bm25_search()'s stats query aggregates over."""
    n_docs = len(docs)
    lengths = {k: len(v) for k, v in docs.items()}
    avgdl = sum(lengths.values()) / n_docs
    scores = {k: 0.0 for k in docs}
    for term in query_terms:
        df = sum(1 for toks in docs.values() if term in toks)
        idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)
        for key, toks in docs.items():
            f = toks.count(term)
            if f == 0:
                continue
            denom = f + BM25_K1 * (1 - BM25_B + BM25_B * lengths[key] / avgdl)
            scores[key] += idf * (f * (BM25_K1 + 1)) / denom
    return scores


def test_bm25_reference_formula_sanity():
    """Pure-math check of the reference formula itself (no DB): a term that
    appears in EVERY document (df == N) must have a strictly lower IDF than a
    term that appears in only one document, for the same corpus size."""
    docs = {"a": ["common", "rare"], "b": ["common"], "c": ["common"]}
    scores_common = _bm25_reference(docs, ["common"])
    scores_rare = _bm25_reference(docs, ["rare"])
    # Both queries hit doc "a" with the same term frequency (1) and the same
    # document length context, so the entire score difference is IDF.
    assert scores_rare["a"] > scores_common["a"] > 0
    assert scores_common["b"] == pytest.approx(scores_common["c"])


# ── Fixture helpers ──────────────────────────────────────────────────────────

async def _company(db, tag: str) -> Company:
    company = Company(name=f"BM25-{tag}", enrollment_token=f"tok-{tag}")
    db.add(company)
    await db.flush()
    return company


async def _role_with_perms(db, company, tag: str, perms: list[tuple[str, str]]) -> Role:
    role = Role(company_id=company.id, name=f"Role-{tag}", scope_type="all", is_system=False)
    db.add(role)
    await db.flush()
    for module, action in perms:
        db.add(RolePermission(role_id=role.id, module=module, action=action))
    await db.flush()
    return role


async def _employee(db, company, tag: str, *, role: Role | None) -> Employee:
    emp = Employee(
        company_id=company.id, email=f"{tag}@example.io", name=f"Tester {tag}",
        role_id=role.id if role else None, password_hash="x",
        is_active=True, is_approved=True,
    )
    db.add(emp)
    await db.flush()
    return emp


def _headers(emp: Employee, company: Company) -> dict:
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


async def _audit_row(db, company, *, action, actor_role="admin", target_type="audittest",
                      target_id, metadata=None):
    row = AuditLog(
        company_id=company.id, actor_employee_id=None, actor_role=actor_role,
        action=action, target_type=target_type, target_id=target_id,
        audit_metadata=metadata,
    )
    db.add(row)
    await db.flush()
    return row


async def _entries(client, company, emp, **params) -> list[dict]:
    r = await client.get("/v1/audit", params=params, headers=_headers(emp, company))
    assert r.status_code == 200, r.text
    return r.json()["entries"]


# ── End-to-end: real formula, hand/independently verified ordering ─────────

@pytest.mark.asyncio
async def test_bm25_orders_by_frequency_and_length_and_excludes_non_matches(client, db_session):
    """Three rows in one company:
      A1: 'focusterm' x3, short doc     -> should outrank A2 for "focusterm"
      A2: 'focusterm' x1, long doc      -> present but lower-ranked than A1
      A3: no 'focusterm' at all         -> excluded from recall entirely
    Verified against an independent Python re-implementation of the formula,
    not against the production code.
    """
    tag = uuid.uuid4().hex[:8]
    company = await _company(db_session, tag)
    role = await _role_with_perms(db_session, company, tag, [("audit", "view")])
    admin = await _employee(db_session, company, tag, role=role)

    action = f"bm25action{tag}"
    a1 = await _audit_row(
        db_session, company, action=action, target_id=f"rowa1{tag}",
        metadata={"note": "focusterm focusterm focusterm everywhereterm raretermonly"},
    )
    a2 = await _audit_row(
        db_session, company, action=action, target_id=f"rowa2{tag}",
        metadata={"note": "focusterm everywhereterm padword padword padword padword padword padword padword padword"},
    )
    a3 = await _audit_row(
        db_session, company, action=action, target_id=f"rowa3{tag}",
        metadata={"note": "everywhereterm unrelatedword"},
    )

    docs = {
        "a1": _row_tokens(action, "admin", "audittest", a1.target_id, {"note": "focusterm focusterm focusterm everywhereterm raretermonly"}),
        "a2": _row_tokens(action, "admin", "audittest", a2.target_id, {"note": "focusterm everywhereterm padword padword padword padword padword padword padword padword"}),
        "a3": _row_tokens(action, "admin", "audittest", a3.target_id, {"note": "everywhereterm unrelatedword"}),
    }

    # ── "focusterm": recall excludes a3, ranks a1 above a2 ──────────────────
    ref = _bm25_reference(docs, ["focusterm"])
    assert ref["a1"] > ref["a2"] > 0
    entries = await _entries(client, company, admin, search="focusterm")
    target_ids = [e["target_id"] for e in entries]
    assert target_ids == [a1.target_id, a2.target_id]

    # ── "everywhereterm": all three match; pure length-normalization test
    # (same term, same frequency=1 everywhere) — shortest doc (a3) ranks
    # highest, longest (a2) ranks lowest.
    ref_common = _bm25_reference(docs, ["everywhereterm"])
    expected_order = sorted(["a1", "a2", "a3"], key=lambda k: -ref_common[k])
    assert expected_order == ["a3", "a1", "a2"]
    entries = await _entries(client, company, admin, search="everywhereterm")
    target_ids = [e["target_id"] for e in entries]
    id_map = {"a1": a1.target_id, "a2": a2.target_id, "a3": a3.target_id}
    assert target_ids == [id_map[k] for k in expected_order]

    # ── discriminating IDF: a term unique to one row (raretermonly, df=1)
    # is far more selective than one present in every row (everywhereterm,
    # df=3) — the common term matches broadly, the rare one is exact.
    r_rare = await client.get("/v1/audit", params={"search": "raretermonly"}, headers=_headers(admin, company))
    r_common = await client.get("/v1/audit", params={"search": "everywhereterm"}, headers=_headers(admin, company))
    assert r_rare.json()["total"] == 1
    assert r_common.json()["total"] == 3
    ref_rare = _bm25_reference(docs, ["raretermonly"])
    ref_common_solo = _bm25_reference(docs, ["everywhereterm"])
    # Same doc (a1) contains both terms once each in the "focusterm" fixture's
    # neighborhood — raretermonly's own top score must exceed everywhereterm's
    # own top score, reflecting IDF(rare) > IDF(common) for this corpus.
    assert max(ref_rare.values()) > max(ref_common_solo.values())

    await db_session.rollback()


@pytest.mark.asyncio
async def test_bm25_sums_contributions_across_multiple_terms(client, db_session):
    """Two rows both contain BOTH query terms, with frequencies swapped and
    equal document length (isolating the summation itself from length
    normalization) plus a third row that breaks the term-frequency symmetry
    so the combined ranking is not a trivial tie."""
    tag = uuid.uuid4().hex[:8]
    company = await _company(db_session, tag)
    role = await _role_with_perms(db_session, company, tag, [("audit", "view")])
    admin = await _employee(db_session, company, tag, role=role)

    action = f"bm25sum{tag}"

    def mk(note):
        return {"note": note}

    s1_note = "sumalpha sumalpha sumalpha sumalpha sumalpha sumbeta"
    s2_note = "sumalpha sumbeta sumbeta sumbeta sumbeta sumbeta"
    s3_note = "sumalpha fillerword"  # breaks df symmetry: sumalpha now in 3 docs, sumbeta in 2

    s1 = await _audit_row(db_session, company, action=action, target_id=f"s1{tag}", metadata=mk(s1_note))
    s2 = await _audit_row(db_session, company, action=action, target_id=f"s2{tag}", metadata=mk(s2_note))
    s3 = await _audit_row(db_session, company, action=action, target_id=f"s3{tag}", metadata=mk(s3_note))

    docs = {
        "s1": _row_tokens(action, "admin", "audittest", s1.target_id, mk(s1_note)),
        "s2": _row_tokens(action, "admin", "audittest", s2.target_id, mk(s2_note)),
        "s3": _row_tokens(action, "admin", "audittest", s3.target_id, mk(s3_note)),
    }

    single_alpha = _bm25_reference(docs, ["sumalpha"])
    single_beta = _bm25_reference(docs, ["sumbeta"])
    combined = _bm25_reference(docs, ["sumalpha", "sumbeta"])

    # The combined score for s1/s2 must equal the SUM of each term's own
    # contribution computed independently — this is exactly what "summing
    # per-term contributions" means, and would fail if the implementation
    # only used e.g. the max or the last term's score instead of a sum.
    for key in ("s1", "s2"):
        assert combined[key] == pytest.approx(single_alpha[key] + single_beta[key])

    expected_order = sorted(["s1", "s2"], key=lambda k: -combined[k])

    entries = await _entries(client, company, admin, search="sumalpha sumbeta")
    target_ids = [e["target_id"] for e in entries]
    id_map = {"s1": s1.target_id, "s2": s2.target_id, "s3": s3.target_id}
    # s3 lacks 'sumbeta' so AND-recall (mirrors build_search's AND-across-
    # tokens semantics) excludes it entirely.
    assert target_ids == [id_map[k] for k in expected_order]

    await db_session.rollback()


# ── Company isolation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bm25_stats_never_cross_company_boundaries(client, db_session):
    """Company B is stuffed with rows that would badly skew N/avgdl/df if the
    stats query weren't scoped correctly. Company A's results/order for the
    same query terms must be identical to the single-company baseline."""
    tag = uuid.uuid4().hex[:8]
    company_a = await _company(db_session, tag + "a")
    role_a = await _role_with_perms(db_session, company_a, tag + "a", [("audit", "view")])
    admin_a = await _employee(db_session, company_a, tag + "a", role=role_a)

    action = f"isoaction{tag}"
    a1 = await _audit_row(
        db_session, company_a, action=action, target_id=f"isoa1{tag}",
        metadata={"note": "isoterm isoterm isoterm"},
    )
    a2 = await _audit_row(
        db_session, company_a, action=action, target_id=f"isoa2{tag}",
        metadata={"note": "isoterm"},
    )

    docs_a_only = {
        "a1": _row_tokens(action, "admin", "audittest", a1.target_id, {"note": "isoterm isoterm isoterm"}),
        "a2": _row_tokens(action, "admin", "audittest", a2.target_id, {"note": "isoterm"}),
    }
    ref_isolated = _bm25_reference(docs_a_only, ["isoterm"])
    expected_order = sorted(["a1", "a2"], key=lambda k: -ref_isolated[k])
    assert expected_order == ["a1", "a2"]

    entries = await _entries(client, company_a, admin_a, search="isoterm")
    id_map = {"a1": a1.target_id, "a2": a2.target_id}
    assert [e["target_id"] for e in entries] == [id_map[k] for k in expected_order]
    assert len(entries) == 2

    # ── Company B: 50 rows all containing 'isoterm' many times each, which
    # would massively inflate N and df(isoterm) (crashing its IDF toward
    # zero) and skew avgdl if the stats query ever leaked across companies.
    company_b = await _company(db_session, tag + "b")
    role_b = await _role_with_perms(db_session, company_b, tag + "b", [("audit", "view")])
    admin_b = await _employee(db_session, company_b, tag + "b", role=role_b)
    for i in range(50):
        await _audit_row(
            db_session, company_b, action=f"noise{tag}", target_id=f"noise{i}{tag}",
            metadata={"note": "isoterm " * 20},
        )

    # Company A's own results must be byte-for-byte unaffected.
    entries_after = await _entries(client, company_a, admin_a, search="isoterm")
    assert [e["target_id"] for e in entries_after] == [id_map[k] for k in expected_order]
    assert len(entries_after) == 2

    # And company B's own search must only ever see its own 50 rows, not A's 2.
    r_b = await client.get("/v1/audit", params={"search": "isoterm"}, headers=_headers(admin_b, company_b))
    assert r_b.status_code == 200
    assert r_b.json()["total"] == 50

    await db_session.rollback()


# ── Empty search / wildcard-safety / permission gate ─────────────────────────

@pytest.mark.asyncio
async def test_empty_search_param_is_unchanged_from_no_search(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = await _company(db_session, tag)
    role = await _role_with_perms(db_session, company, tag, [("audit", "view")])
    admin = await _employee(db_session, company, tag, role=role)

    action = f"emptycheck{tag}"
    await _audit_row(db_session, company, action=action, target_id=f"e1{tag}")
    await _audit_row(db_session, company, action=action, target_id=f"e2{tag}")

    r_omitted = await client.get("/v1/audit", headers=_headers(admin, company))
    r_empty = await client.get("/v1/audit", params={"search": ""}, headers=_headers(admin, company))
    assert r_omitted.status_code == 200
    assert r_empty.status_code == 200
    # No BM25 machinery invoked for either — same total, same row order
    # (created_at DESC, the pre-existing fallback ordering).
    assert r_omitted.json()["total"] == r_empty.json()["total"]
    ids_omitted = [e["id"] for e in r_omitted.json()["entries"]]
    ids_empty = [e["id"] for e in r_empty.json()["entries"]]
    assert ids_omitted == ids_empty

    await db_session.rollback()


@pytest.mark.asyncio
async def test_literal_percent_underscore_quote_do_not_500_or_wildcard_match(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = await _company(db_session, tag)
    role = await _role_with_perms(db_session, company, tag, [("audit", "view")])
    admin = await _employee(db_session, company, tag, role=role)

    action = f"wildcardcheck{tag}"
    await _audit_row(db_session, company, action=action, target_id=f"w1{tag}", metadata={"note": "harmless content"})
    await _audit_row(db_session, company, action=action, target_id=f"w2{tag}", metadata={"note": "more harmless content"})

    for literal in ["%", "_", "'", "%_'"]:
        r = await client.get("/v1/audit", params={"search": literal}, headers=_headers(admin, company))
        assert r.status_code == 200, f"search={literal!r} should not 500, got {r.status_code}: {r.text}"
        # These are pure punctuation with no lexemes once tokenized by
        # Postgres's parser — plainto_tsquery produces an empty tsquery, and
        # `tsvector @@ ''::tsquery` is false, so nothing matches (verified
        # directly against live Postgres 16). Must NOT match every row.
        assert r.json()["total"] == 0, f"search={literal!r} matched every row (wildcard leak)"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_audit_view_permission_still_gates_search(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = await _company(db_session, tag)
    # No role / no audit:view permission at all.
    no_perm_emp = await _employee(db_session, company, tag, role=None)

    r = await client.get("/v1/audit", params={"search": "anything"}, headers=_headers(no_perm_emp, company))
    assert r.status_code == 403

    await db_session.rollback()
