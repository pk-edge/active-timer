# TimeTracker (active-time)

Monorepo: desktop agent (Python/PySide6), FastAPI backend, Next.js dashboard.

## Codebase guide

`docs/CODEBASE_GUIDE.md` is this project's living codebase reference doc.

- **Read** the relevant section before making any non-trivial change.
- **Update** it in the same turn as any change that alters architecture,
  adds/removes a module, or changes a cross-cutting pattern (auth, data
  isolation, background jobs, caching, migrations).

## Permissions are part of every feature

No feature is complete until it is wired into RBAC, in the same change:

1. The `(module, action)` pair exists in `backend/app/core/permissions.py`
   (add the module or action there if none fits).
2. The endpoint is gated with `Depends(require("<module>", "<action>"))`.
3. The UI hides or disables the trigger behind `can("<module>", "<action>")`.

Backend-only gating is not enough — a visible button that 403s is a bug. When a
feature lives on one page but calls another module's API, gate it on the module
that owns the API (report buttons on the Devices page check `reports:view`).
See "RBAC" in `docs/CODEBASE_GUIDE.md` for the full rule.
