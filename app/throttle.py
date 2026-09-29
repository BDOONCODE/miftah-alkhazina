"""حد لمحاولات الدخول الفاشلة (حماية من تخمين كلمات المرور).

بالذاكرة، ويكفي لخادم واحد مثل الاستضافة المجانية.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

MAX_FAILURES = 5
WINDOW_SECONDS = 10 * 60

_failures: dict[str, deque[float]] = defaultdict(deque)


def _key(ip: str, username: str) -> str:
    return f"{ip}|{username.strip().lower()}"


def _prune(bucket: deque[float], now: float) -> None:
    while bucket and now - bucket[0] > WINDOW_SECONDS:
        bucket.popleft()


def is_blocked(ip: str, username: str, now: float | None = None) -> bool:
    now = now or time.monotonic()
    bucket = _failures[_key(ip, username)]
    _prune(bucket, now)
    return len(bucket) >= MAX_FAILURES


def record_failure(ip: str, username: str, now: float | None = None) -> None:
    _failures[_key(ip, username)].append(now or time.monotonic())


def reset(ip: str, username: str) -> None:
    _failures.pop(_key(ip, username), None)
