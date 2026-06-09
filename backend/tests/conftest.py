"""pytest bootstrap for backend RBAC/scoping tests.

Adds the backend root to sys.path so `import app...` resolves when running
`pytest` from the container's /app working directory.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest_plugins = ("pytest_asyncio",)
