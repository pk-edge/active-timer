"""Unit tests for ConfigWorker — the agent's headless poller. The widget's
hide/show is a thin Qt shell (device-tested); the polling logic is what matters
here: parse the flag, never flip on a failed poll, no-op until enrolled."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_worker
from config_worker import ConfigWorker


class FakeResp:
    def __init__(self, data, status=200):
        self._d = data
        self.status_code = status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def get(self, url, headers=None):
        if self._exc:
            raise self._exc
        return self._resp

    def close(self):
        pass


def _worker(shared, client):
    w = ConfigWorker(shared)
    w._http = client          # swap the real httpx client for the fake
    return w


def test_poll_sets_headless(monkeypatch):
    monkeypatch.setattr(config_worker.identity, "load", lambda: {"jwt_token": "t"})
    shared = {"stop_event": threading.Event(), "headless": False}
    _worker(shared, FakeClient(resp=FakeResp({"headless": True})))._poll_once()
    assert shared["headless"] is True


def test_poll_keeps_last_value_on_error(monkeypatch):
    monkeypatch.setattr(config_worker.identity, "load", lambda: {"jwt_token": "t"})
    shared = {"stop_event": threading.Event(), "headless": True}
    _worker(shared, FakeClient(exc=RuntimeError("boom")))._poll_once()
    assert shared["headless"] is True   # a failed poll must never flip visibility


def test_poll_noop_until_enrolled(monkeypatch):
    monkeypatch.setattr(config_worker.identity, "load", lambda: {})  # no jwt yet

    class Boom:
        def get(self, *a, **k):
            raise AssertionError("must not call the API before enrollment")
        def close(self):
            pass

    shared = {"stop_event": threading.Event(), "headless": False}
    _worker(shared, Boom())._poll_once()
    assert shared["headless"] is False
