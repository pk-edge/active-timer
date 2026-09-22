"""Shared search helper (app/core/search.py): escaping, tokenized AND/OR
matching, accent folding, and relevance ranking — plus a couple of
end-to-end checks that the refactored endpoints still behave for a plain
query.

DB-touching tests run against the live Postgres in a never-committed session
(same pattern as test_device_roster_report.py / test_impersonation.py),
tagged with uuid.uuid4().hex[:8] so fixture rows never collide across runs.
"""
import uuid

import pytest
from sqlalchemy import text

from app.core.search import build_search, escape_like
from app.core.security import create_admin_token
from app.db.models import AuditLog, Company, Employee, Role


# ── Pure-Python: escape_like ──────────────────────────────────────────────────

def test_escape_like_neutralises_percent_and_underscore():
    assert escape_like("%") == "\\%"
    assert escape_like("_") == "\\_"
    assert escape_like("100%_done") == "100\\%\\_done"


def test_escape_like_escapes_backslash_first():
    # A literal backslash must itself be escaped, and escaping order matters:
    # escaping backslash AFTER escaping % / _ would double-escape the
    # backslashes just inserted for them.
    assert escape_like("\\") == "\\\\"
    assert escape_like("a\\_b") == "a\\\\\\_b"


def test_build_search_empty_query_is_a_no_op():
    sc = build_search(None, ["employees.name"])
    assert sc.where == "" and sc.order_by == "" and sc.params == {}

    sc = build_search("   ", ["employees.name"])
    assert sc.where == "" and sc.order_by == ""


def test_build_search_caps_token_count():
    sc = build_search("a b c d e f g h i j k", ["employees.name"], max_tokens=8)
    # 8 tokens => 8 distinct bound "t{n}" params, not 11.
    tok_params = [k for k in sc.params if "_t" in k]
    assert len(tok_params) == 8


# ── DB-backed: literal wildcard input, AND semantics, accents, ranking ───────

async def _company_and_employees(db, tag: str, names_emails: list[tuple[str, str]]):
    company = Company(name=f"Search-{tag}", enrollment_token=f"tok-{tag}")
    db.add(company)
    await db.flush()
    emps = []
    for name, email in names_emails:
        e = Employee(company_id=company.id, email=f"{tag}-{email}", name=name)
        db.add(e)
        emps.append(e)
    await db.flush()
    return company, emps


async def _search_names(db, company_id, query: str, **kw) -> list[str]:
    sc = build_search(query, ["employees.name", "employees.email"], **kw)
    where = f"employees.company_id = :cid" + (f" AND {sc.where}" if sc.where else "")
    order = f"ORDER BY {sc.order_by}, employees.name" if sc.order_by else "ORDER BY employees.name"
    params = {"cid": str(company_id), **sc.params}
    rows = await db.execute(
        text(f"SELECT name FROM employees WHERE {where} {order}"), params
    )
    return [r[0] for r in rows.fetchall()]


@pytest.mark.asyncio
async def test_literal_percent_query_no_longer_matches_everything(db_session):
    tag = uuid.uuid4().hex[:8]
    company, emps = await _company_and_employees(
        db_session, tag,
        [("Alice Example", "alice@example.io"), ("Bob Builder", "bob@example.io")],
    )

    # Before escaping, "%" as a LIKE pattern matches every row. After escaping
    # it is a literal percent sign, which appears in none of these names.
    names = await _search_names(db_session, company.id, "%")
    assert names == []

    await db_session.rollback()


@pytest.mark.asyncio
async def test_literal_underscore_is_not_a_single_char_wildcard(db_session):
    tag = uuid.uuid4().hex[:8]
    company, emps = await _company_and_employees(
        db_session, tag,
        [("A_B Underscore", "underscore@example.io"), ("AxB Wildcard", "wildcard@example.io")],
    )

    # "_" should only match the literal underscore in "A_B", not stand in for
    # any single character (which would also match "AxB").
    names = await _search_names(db_session, company.id, "A_B")
    assert names == ["A_B Underscore"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_multi_token_requires_all_tokens_and_is_order_independent(db_session):
    tag = uuid.uuid4().hex[:8]
    company, emps = await _company_and_employees(
        db_session, tag,
        [
            ("Agha Khan", "agha.khan@example.io"),
            ("Agha Solo", "agha.solo@example.io"),
            ("Someone Khan", "someone.khan@example.io"),
        ],
    )

    names = await _search_names(db_session, company.id, "khan agha")
    assert names == ["Agha Khan"]

    # A single shared token matches all rows containing it.
    names = await _search_names(db_session, company.id, "agha")
    assert set(names) == {"Agha Khan", "Agha Solo"}

    await db_session.rollback()


@pytest.mark.asyncio
async def test_accent_folding_matches_unaccented_query(db_session):
    tag = uuid.uuid4().hex[:8]
    company, emps = await _company_and_employees(
        db_session, tag,
        [("José García", "jose@example.io"), ("Jose Nobody", "josen@example.io")],
    )

    names = await _search_names(db_session, company.id, "jose garcia")
    assert names == ["José García"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_ranking_puts_exact_match_above_fuzzy_match(db_session):
    tag = uuid.uuid4().hex[:8]
    company, emps = await _company_and_employees(
        db_session, tag,
        [
            ("Mohammad Ali", f"mohammadali-{tag}@example.io"),
            ("Mohammed Farooq", f"mohammedfarooq-{tag}@example.io"),  # similar spelling
            ("Mohammad", f"mohammadexact-{tag}@example.io"),          # exact name match
        ],
    )

    # A single token "mohammad" substring-matches all three names (contained
    # literally, or via the trigram-similar "Mohammed"/"Mohammad Ali"), but
    # the row whose name IS exactly "Mohammad" must rank first.
    names = await _search_names(db_session, company.id, "mohammad")
    assert names[0] == "Mohammad", f"expected exact match first, got {names}"

    await db_session.rollback()


# ── Endpoint-level: plain-query parity after refactor ────────────────────────

def _admin_headers(emp, company):
    return {"Authorization": f"Bearer {create_admin_token(str(emp.id), emp.role, str(company.id))}"}


@pytest.mark.asyncio
async def test_users_list_search_plain_query_still_matches_name_and_email(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"Search-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_role = await _all_scope_role(db_session, company, tag)
    admin = Employee(
        company_id=company.id, email=f"admin-{tag}@example.io", name="Admin User",
        role_id=admin_role.id, password_hash="x", is_active=True, is_approved=True,
    )
    target = Employee(
        company_id=company.id, email=f"findme-{tag}@example.io", name="Findme Target",
        is_active=True, is_approved=True,
    )
    other = Employee(
        company_id=company.id, email=f"other-{tag}@example.io", name="Other Person",
        is_active=True, is_approved=True,
    )
    db_session.add_all([admin, target, other])
    await db_session.flush()

    r = await client.get("/v1/users", params={"search": "findme"}, headers=_admin_headers(admin, company))
    assert r.status_code == 200
    names = {u["name"] for u in r.json()}
    assert "Findme Target" in names
    assert "Other Person" not in names

    await db_session.rollback()


async def _all_scope_role(db, company, tag):
    from app.core.permissions import all_permissions

    role = Role(company_id=company.id, name=f"Admin-{tag}", scope_type="all", is_system=False)
    db.add(role)
    await db.flush()
    from app.db.models import RolePermission
    for module, action in all_permissions():
        db.add(RolePermission(role_id=role.id, module=module, action=action))
    await db.flush()
    return role


@pytest.mark.asyncio
async def test_shared_files_search_plain_query_still_works(client, db_session):
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"Search-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_role = await _all_scope_role(db_session, company, tag)
    admin = Employee(
        company_id=company.id, email=f"admin2-{tag}@example.io", name="Admin Two",
        role_id=admin_role.id, password_hash="x", is_active=True, is_approved=True,
    )
    db_session.add(admin)
    await db_session.flush()

    from app.db.models import SharedFile
    f1 = SharedFile(
        company_id=company.id, uploaded_by=admin.id, original_filename=f"report-{tag}.pdf",
        s3_key=f"shared-files/{company.id}/{tag}_report.pdf", mime_type="application/pdf",
        file_size_bytes=100, public_token=uuid.uuid4().hex,
    )
    f2 = SharedFile(
        company_id=company.id, uploaded_by=admin.id, original_filename=f"unrelated-{tag}.docx",
        s3_key=f"shared-files/{company.id}/{tag}_unrelated.docx", mime_type="application/msword",
        file_size_bytes=100, public_token=uuid.uuid4().hex,
    )
    db_session.add_all([f1, f2])
    await db_session.flush()

    r = await client.get("/v1/files", params={"q": "report"}, headers=_admin_headers(admin, company))
    assert r.status_code == 200
    body = r.json()
    filenames = {item["original_filename"] for item in body["items"]}
    assert f"report-{tag}.pdf" in filenames
    assert f"unrelated-{tag}.docx" not in filenames

    await db_session.rollback()


@pytest.mark.asyncio
async def test_audit_search_plain_query_matches_action_and_no_longer_leaks_on_wildcard(
    client, db_session,
):
    """The worst offender pre-fix: `a.metadata::text ILIKE '%raw%'` with no
    escaping. Confirms a plain query still finds the right row (parity) and a
    literal '%' no longer returns every row in the company (the bug)."""
    tag = uuid.uuid4().hex[:8]
    company = Company(name=f"Search-{tag}", enrollment_token=f"tok-{tag}")
    db_session.add(company)
    await db_session.flush()

    admin_role = await _all_scope_role(db_session, company, tag)
    admin = Employee(
        company_id=company.id, email=f"admin3-{tag}@example.io", name="Admin Three",
        role_id=admin_role.id, password_hash="x", is_active=True, is_approved=True,
    )
    db_session.add(admin)
    await db_session.flush()

    hit = AuditLog(
        company_id=company.id, actor_employee_id=admin.id, actor_role="admin",
        action=f"user.create-{tag}", target_type="user", target_id="target-1",
    )
    other = AuditLog(
        company_id=company.id, actor_employee_id=admin.id, actor_role="admin",
        action=f"user.delete-{tag}", target_type="user", target_id="target-2",
    )
    db_session.add_all([hit, other])
    await db_session.flush()

    # Plain-query parity: searching the distinguishing term finds only the
    # matching row. Audit search moved from ILIKE-substring to BM25 (see
    # app/core/search.py::build_bm25_search / CODEBASE_GUIDE.md "Search"),
    # which recalls on whole tsvector lexemes, not arbitrary substrings — the
    # 'simple' parser fuses a dotted compound like "user.create-<tag>" into
    # lexemes 'user.create' and '<tag>' (verified against live Postgres 16),
    # with no standalone 'create' lexeme. So the search term here must be a
    # real lexeme (or a plainto_tsquery-AND of several), not an arbitrary
    # substring — "create-{tag}" alone can never match under BM25 tokenization,
    # whereas the full compound "user.create-{tag}" AND-matches both of its
    # lexemes and correctly excludes `other` (which has 'user.delete', not
    # 'user.create', despite sharing the same tag lexeme).
    r = await client.get(
        "/v1/audit", params={"search": f"user.create-{tag}"}, headers=_admin_headers(admin, company)
    )
    assert r.status_code == 200
    body = r.json()
    actions = {e["action"] for e in body["entries"]}
    assert f"user.create-{tag}" in actions
    assert f"user.delete-{tag}" not in actions

    # The bug: searching a literal '%' must NOT match every audit row in the
    # company (it used to, via unescaped f"%{search}%").
    r = await client.get("/v1/audit", params={"search": "%"}, headers=_admin_headers(admin, company))
    assert r.status_code == 200
    assert r.json()["total"] == 0

    await db_session.rollback()
