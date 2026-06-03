"""Single source of truth for the RBAC permission matrix.

Adding a module here is the only change needed to expose it across the API and
the dashboard UI (the UI fetches this catalog via GET /v1/roles/modules).
"""
from typing import TypedDict


class ModuleDef(TypedDict):
    key: str
    label: str
    actions: list[str]


MODULES: list[ModuleDef] = [
    {"key": "devices",     "label": "Devices",       "actions": ["view"]},
    {"key": "logs",        "label": "Activity Logs", "actions": ["view"]},
    {"key": "reports",     "label": "Reports",       "actions": ["view"]},
    {"key": "screenshots", "label": "Screenshots",   "actions": ["view"]},
    {"key": "users",       "label": "Users",         "actions": ["view", "create", "edit", "delete"]},
    {"key": "groups",      "label": "Groups",        "actions": ["view", "create", "edit", "delete"]},
    {"key": "roles",       "label": "Roles",         "actions": ["view", "create", "edit", "delete"]},
    {"key": "agents",      "label": "Agents",        "actions": ["view", "create", "edit", "delete"]},
    {"key": "settings",    "label": "Settings",      "actions": ["view", "edit"]},
    {"key": "audit",       "label": "Audit Log",     "actions": ["view"]},
]

_VALID: set[tuple[str, str]] = {(m["key"], a) for m in MODULES for a in m["actions"]}


def all_permissions() -> list[tuple[str, str]]:
    """Every (module, action) pair — used to seed Super Admin."""
    return sorted(_VALID)


def is_valid(module: str, action: str) -> bool:
    return (module, action) in _VALID
