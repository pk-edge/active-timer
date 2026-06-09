# Self-Registration with Admin Approval — Design

**Date:** 2026-06-04
**Status:** Approved
**Scope:** Backend (FastAPI/PostgreSQL), Frontend (Next.js 14)

---

## 1. Goal

Allow people to sign up via a public `/register` page (name, email, password). New self-registered accounts are **pending** — they cannot log in until an admin approves them and assigns a role. Existing admin-created accounts are unaffected.

## 2. Decisions

- **Model:** Register → pending approval (admin-controlled access, no open self-grant).
- **Auth:** Email/password only. Google sign-in deferred.
- **Company:** Single-company deployment; registrations attach to the one Company row.
- **No role at registration:** Pending users have `role_id = NULL`. Admin assigns role + groups on approval. Rejection = deactivate (existing action).

## 3. Database — `003_register.sql`

One new column on `employees`:

```sql
ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE;
```

`DEFAULT TRUE` so every existing employee (admin-created, legacy) stays fully approved and unchanged.

Partial index for efficient queue query:
```sql
CREATE INDEX IF NOT EXISTS idx_employees_pending ON employees(company_id) WHERE is_approved = FALSE AND is_active = TRUE;
```

## 4. Backend

### 4.1 `POST /v1/auth/register` (public — no auth)

Request body: `{name: str, email: EmailStr, password: str}` (minimum 8 chars enforced by Pydantic).

Logic:
1. Find the single Company (first row — single-company deployment).
2. Reject duplicate email → 409.
3. Create `Employee(company_id, email, name, role='employee', password_hash=hash(password), is_active=True, is_approved=False, role_id=None)`.
4. Write `audit_log` entry: `action='user.register'`.
5. Return `201 {message: "Registration submitted. You will be notified once an admin approves your account."}` — **no token**.

### 4.2 Login change (`POST /v1/auth/admin/login`)

Current check: active + has_dashboard_role + password_hash + verify. Add one gate **before** issuing the token:

```
if not employee.is_approved:
    raise HTTPException(403, "Account pending admin approval")
```

### 4.3 `GET /v1/users?pending=true` (users.view)

Existing endpoint gains an optional `pending: bool = False` query param. When `pending=True`, return only `is_approved=False AND is_active=True` rows (the approval queue). `UserOut` gains `is_approved: bool`.

### 4.4 `POST /v1/users/{id}/approve` (users.edit)

Body: `{role_id: str, group_ids: list[str]}` (both required — admin must assign a role on approval).

Logic:
1. Load employee, assert belongs to same company.
2. Resolve and assign `role_id` + `employee_group_access`.
3. Set `is_approved=True`.
4. Audit: `action='user.approve'`, metadata: `{email, role_name}`.
5. Return updated `UserOut`.

## 5. Schemas

- `RegisterRequest`: `name: str`, `email: EmailStr`, `password: str` (min_length=8).
- `RegisterResponse`: `message: str`.
- `UserOut` gains: `is_approved: bool`.
- `ApproveRequest`: `role_id: str`, `group_ids: list[str] = []`.

## 6. Frontend

### 6.1 `/register` page (auth group, public)

Fields: Full name, Email, Password (min 8 chars), Confirm password (client-side match check). On submit:
- Calls `POST /v1/auth/register`.
- On 201 → shows a success state: "Request submitted! An admin will review your account." + link back to Login.
- On 409 → inline "This email is already registered."
- On error → inline error message.
- Logo + light-mode card matching the existing login page style.
- "Already have an account? Sign in" link to `/login`.

### 6.2 Login page changes

- Add "Don't have an account? **Register**" link below the button.
- Detect HTTP 403 with "pending admin approval" message body → show a specific message: *"Your account is awaiting admin approval."* (distinct from wrong password 401).

### 6.3 Users page — Pending tab

- Add a **"Pending"** tab pill next to the search bar. Shows count badge (e.g., `Pending (3)`) when queue is non-empty.
- Pending tab shows unapproved employees: name, email, registered-at, no role.
- Row action: **Approve** → opens a modal with role selector (required) + optional group multiselect. Calls `/v1/users/{id}/approve`.
- Row action: **Reject** → calls existing `DELETE /v1/users/{id}` (deactivate). With confirmation dialog.
- Active tab hides `is_approved=False` rows (they belong in Pending only).

## 7. API client additions

```ts
registerUser(body: {name, email, password}) → POST /v1/auth/register
approveUser(id, body: {role_id, group_ids}) → POST /v1/users/{id}/approve
getUsersPending() → GET /v1/users?pending=true
```

## 8. Error handling

| Situation | Response |
|-----------|----------|
| Duplicate email on register | 409 "Email already registered" |
| Pending account logs in | 403 "Account pending admin approval" |
| Approve with no role | 422 from Pydantic (role_id required) |
| Approve unknown user / cross-company | 404 |
| Wrong password | 401 "Invalid credentials" (unchanged) |

## 9. Out of scope

- Email notification to the user on approval (no SMTP configured).
- Email notification to admin on new registration (same).
- Google / SSO login (deferred).
- Multi-company registration (single-company deployment).
