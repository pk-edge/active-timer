module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'scope-enum': [
      2,
      'always',
      [
        // ── Stack areas ──────────────────────────────────────────────────
        'agent',
        'backend',
        'dashboard',
        'installer',
        // ── Feature modules (mirrors backend/app/core/permissions.py) ───
        'home',
        'overview',
        'team',
        'activity',
        'categories',
        'devices',
        'logs',
        'reports',
        'tech',
        'users',
        'groups',
        'roles',
        'policy',
        'audit',
        'settings',
        'files',
        // ── Cross-cutting ────────────────────────────────────────────────
        'auth',
        'docs',
        'ci',
        'deps',
        'release',
      ],
    ],
  },
};
