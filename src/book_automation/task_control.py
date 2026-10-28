# -*- coding: utf-8 -*-
"""Shared runtime controls for Web-triggered background tasks."""
from __future__ import annotations

import threading
from contextlib import contextmanager


class CancelledTask(RuntimeError):
    """Raised when the user requests the current background task to stop."""


_cancel_event = threading.Event()
_resources_lock = threading.Lock()
_browsers: set[object] = set()


def reset_cancel():
    _cancel_event.clear()


def request_cancel():
    _cancel_event.set()
    close_registered_browsers()


def is_cancelled() -> bool:
    return _cancel_event.is_set()


def raise_if_cancelled():
    if is_cancelled():
        raise CancelledTask("用户请求停止当前任务")


def register_browser(browser):
    with _resources_lock:
        _browsers.add(browser)


def unregister_browser(browser):
    with _resources_lock:
        _browsers.discard(browser)


def close_registered_browsers():
    with _resources_lock:
        browsers = list(_browsers)
    for browser in browsers:
        try:
            browser.close()
        except Exception:
            pass


@contextmanager
def tracked_browser(browser):
    register_browser(browser)
    try:
        yield browser
    finally:
        unregister_browser(browser)
