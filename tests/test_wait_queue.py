"""Tests for the in-process wait queue."""

from __future__ import annotations

import threading
import time

import pytest

from reqable_mcp.wait_queue import FilterSpec, WaitQueue

# --------------------------------------------------------------- FilterSpec


def _cap(**kwargs):
    base = {
        "uid": "u",
        "host": "example.com",
        "method": "GET",
        "status": 200,
        "url": "https://example.com/api/v1/users",
        "path": "/api/v1/users",
        "app_name": "Chrome",
    }
    base.update(kwargs)
    return base


def test_filter_no_constraints_matches_anything() -> None:
    fs = FilterSpec()
    assert fs.matches(_cap()) is True


def test_filter_host_exact() -> None:
    fs = FilterSpec(host="example.com")
    assert fs.matches(_cap(host="example.com")) is True
    assert fs.matches(_cap(host="other.org")) is False


def test_filter_method_normalized_uppercase() -> None:
    """``FilterSpec(method='post')`` should match POST captures."""
    fs = FilterSpec(method="post")
    assert fs.matches(_cap(method="POST")) is True
    assert fs.matches(_cap(method="GET")) is False


def test_filter_status_exact() -> None:
    fs = FilterSpec(status=404)
    assert fs.matches(_cap(status=404)) is True
    assert fs.matches(_cap(status=200)) is False


def test_filter_app_exact() -> None:
    fs = FilterSpec(app="Chrome")
    assert fs.matches(_cap(app_name="Chrome")) is True
    assert fs.matches(_cap(app_name="Safari")) is False


def test_filter_path_pattern_regex() -> None:
    fs = FilterSpec(path_pattern=r"/api/v\d+/users")
    assert fs.matches(_cap(url="https://x.com/api/v1/users")) is True
    assert fs.matches(_cap(url="https://x.com/api/v2/users/42")) is True
    assert fs.matches(_cap(url="https://x.com/static/img.png")) is False


def test_filter_path_pattern_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid path_pattern"):
        FilterSpec(path_pattern="(unclosed")


def test_filter_combined_and() -> None:
    fs = FilterSpec(host="example.com", method="POST", status=200)
    assert fs.matches(_cap(host="example.com", method="POST", status=200)) is True
    assert fs.matches(_cap(host="example.com", method="POST", status=500)) is False
    assert fs.matches(_cap(host="other.org", method="POST", status=200)) is False


# --------------------------------------------------------------- WaitQueue


def test_add_and_wait_returns_match() -> None:
    q = WaitQueue()
    wid = q.add(FilterSpec(host="example.com"))
    assert q.active_count() == 1

    cap = _cap(host="example.com")
    woken = q.notify(cap)
    assert woken == 1
    # Cleanup happens on wait(), not notify().
    result = q.wait(wid, timeout=0.1)
    assert result == cap
    assert q.active_count() == 0


def test_wait_times_out() -> None:
    q = WaitQueue()
    wid = q.add(FilterSpec(host="never.example.com"))
    t0 = time.time()
    result = q.wait(wid, timeout=0.05)
    elapsed = time.time() - t0
    assert result is None
    assert 0.04 <= elapsed <= 0.20  # Allow generous CI variance
    assert q.active_count() == 0  # cleaned up


def test_notify_only_wakes_matching_waiters() -> None:
    q = WaitQueue()
    wid_chrome = q.add(FilterSpec(app="Chrome"))
    wid_safari = q.add(FilterSpec(app="Safari"))

    woken = q.notify(_cap(app_name="Chrome"))
    assert woken == 1

    chrome_result = q.wait(wid_chrome, timeout=0.1)
    safari_result = q.wait(wid_safari, timeout=0.05)  # times out
    assert chrome_result is not None
    assert safari_result is None


def test_notify_to_no_match_returns_zero() -> None:
    q = WaitQueue()
    q.add(FilterSpec(host="example.com"))
    woken = q.notify(_cap(host="other.org"))
    assert woken == 0
    assert q.active_count() == 1  # still pending


def test_notify_is_broadcast_to_all_matching_waiters() -> None:
    """A single capture wakes every matching waiter (broadcast).

    Each waiter only consumes its first match — the *first* notify
    that matches signals it. Later notifies don't re-fire it.
    """
    q = WaitQueue()
    wid1 = q.add(FilterSpec(host="x.com"))
    wid2 = q.add(FilterSpec(host="x.com"))

    cap1 = _cap(host="x.com", uid="a")
    woken = q.notify(cap1)
    assert woken == 2  # both waiters wake on the same capture

    r1 = q.wait(wid1, timeout=0.1)
    r2 = q.wait(wid2, timeout=0.1)
    assert r1["uid"] == "a"
    assert r2["uid"] == "a"


def test_waiter_only_fires_once() -> None:
    """A waiter is signaled by the first matching capture; later
    matching captures don't re-trigger it."""
    q = WaitQueue()
    wid = q.add(FilterSpec(host="x.com"))

    cap1 = _cap(host="x.com", uid="a")
    cap2 = _cap(host="x.com", uid="b")
    woken1 = q.notify(cap1)
    woken2 = q.notify(cap2)
    assert woken1 == 1
    assert woken2 == 0  # already signaled

    r = q.wait(wid, timeout=0.1)
    assert r["uid"] == "a"


def test_wait_blocks_until_notified() -> None:
    q = WaitQueue()
    wid = q.add(FilterSpec(host="example.com"))

    received = {}

    def waiter():
        received["result"] = q.wait(wid, timeout=2.0)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.05)  # let waiter start blocking
    q.notify(_cap(host="example.com"))
    th.join(timeout=1.0)
    assert "result" in received
    assert received["result"] is not None


def test_cancel_idempotent() -> None:
    q = WaitQueue()
    wid = q.add(FilterSpec())
    q.cancel(wid)
    q.cancel(wid)  # should not raise
    assert q.active_count() == 0


def test_cancel_then_wait_returns_none() -> None:
    q = WaitQueue()
    wid = q.add(FilterSpec())
    q.cancel(wid)
    assert q.wait(wid, timeout=0.05) is None


def test_reap_expired_drops_old_waiters() -> None:
    q = WaitQueue()
    q.add(FilterSpec(host="x.com"))
    q.add(FilterSpec(host="y.com"))

    # Force one waiter to look stale by manipulating creation time
    with q._lock:
        for w in q._waiters.values():
            w.created_ts = time.time() - 100

    removed = q.reap_expired(max_age_s=10)
    assert removed == 2
    assert q.active_count() == 0


def test_reap_keeps_fresh_waiters() -> None:
    q = WaitQueue()
    q.add(FilterSpec())
    removed = q.reap_expired(max_age_s=10)
    assert removed == 0
    assert q.active_count() == 1
