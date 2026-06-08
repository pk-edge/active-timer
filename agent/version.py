"""Single source of truth for the agent version.

Bump this when publishing a new build; the backend's /v1/updates/check compares
it against the active installer version to decide whether to self-update.
"""
__version__ = "1.0.2"
