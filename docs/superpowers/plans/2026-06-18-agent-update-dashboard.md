# Agent Auto-Update — Dashboard (SP5) Implementation Plan

> Execute task-by-task. Verify with `npx tsc --noEmit` (typecheck) in `dashboard/`.
> Visual review is the stakeholder's.

**Goal:** Make per-company distribution configurable + observable on the Agents page: rollout %, mandatory, channel, min-supported-version, expiry (editable, persisted via `PATCH /v1/installers/{id}`), plus a fleet view (version adoption + recent update runs from `GET /v1/updates/runs`). Rollback = activate a previous version (existing endpoint).

**Architecture:** Additive. Extend the backend `InstallerOut` (so the list endpoint exposes the distribution fields), add two API-client fns + types, and add two UI blocks to the existing Agents page. Everything stays scoped to the admin's company (per D1/D2).

## Global Constraints
- Additive: existing Agents page features (upload/activate/delete/download/links/history) untouched. New `InstallerOut` fields are additive (existing consumers ignore them).
- `can("agents","edit")` gates distribution edits; `can("agents","view")` gates the fleet view.
- Follow existing patterns: `useSWR`, `request<T>`, Tailwind `brand-*`, lucide icons.

---

### Task 1: Backend — expose distribution fields on `InstallerOut` (additive)
**Files:** Modify `backend/app/schemas/rbac.py` (InstallerOut), `backend/app/api/v1/endpoints/installers.py` (`_out`).
- Add to `InstallerOut`: `rollout_percent: int = 100`, `mandatory: bool = False`, `channel: str = "stable"`, `min_supported_version: Optional[str] = None`, `expires_at: Optional[str] = None`.
- In `_out(v)`: pass those from the row (`expires_at` as `.isoformat()` or None).
- Verify backward-compat: `docker compose run --rm --no-deps -v "$PWD/backend:/app" backend python -m pytest tests/test_e2eApplication.py::test_13_installers_updates_company tests/test_agent_update.py -q` → green.

### Task 2: Dashboard API client + types
**Files:** Modify `dashboard/src/types/index.ts`, `dashboard/src/lib/api.ts`.
- `Installer`: add optional `rollout_percent?`, `mandatory?`, `channel?`, `min_supported_version?`, `expires_at?`.
- Add `UpdateRun` (`id, device_id, from_version, target_version, status, error_code, progress, updated_at`) and `UpdateRunsResponse` (`runs: UpdateRun[]; adoption: Record<string, number>`).
- api.ts: `patchInstaller(id, body)` → `PATCH /v1/installers/{id}`; `getUpdateRuns()` → `GET /v1/updates/runs`.

### Task 3: Agents page — Distribution editor + Fleet status
**Files:** Modify `dashboard/src/app/(dashboard)/agents/page.tsx`.
- `DistributionEditor({ installer, onSaved })`: rollout % (range + number), mandatory (checkbox), channel (select stable/beta), min-version (text), expires (datetime-local/text). Save → `patchInstaller`. Shown per active build (and any row, via expand) when `can("agents","edit")`.
- `FleetStatus()`: `useSWR("update-runs", getUpdateRuns)`; render adoption bars (version → count) + a recent-runs table (device, from→target, status badge, progress, error). Shown when `can("agents","view")`.
- Wire both into the page; keep all existing sections.
- Verify: `cd dashboard && npx tsc --noEmit` → no errors.

## Self-Review
- SP5 spec (§9): per-company distribution controls + fleet adoption/update-status, scoped to admin company. Rollback via existing activate. Additive `InstallerOut`. Types match Plan A PATCH payload + `/runs` response.
