"""Unit tests for the RBAC permission catalog and gate logic."""
import pytest

from app.core.permissions import is_valid, all_permissions
from app.api.deps import _has_permission


def test_catalog_has_core_modules():
    assert is_valid("users", "create")
    assert is_valid("devices", "view")
    assert not is_valid("users", "explode")
    assert ("roles", "delete") in all_permissions()


def test_all_permissions_is_sorted_and_unique():
    perms = all_permissions()
    assert perms == sorted(perms)
    assert len(perms) == len(set(perms))


@pytest.mark.parametrize("perms,module,action,allowed", [
    ({("users", "view")}, "users", "view", True),
    ({("users", "view")}, "users", "delete", False),
    (set(), "devices", "view", False),
    ({("devices", "view"), ("logs", "view")}, "logs", "view", True),
])
def test_permission_check(perms, module, action, allowed):
    assert _has_permission(perms, module, action) is allowed
