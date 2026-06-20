"""
MYTHOS — virtual clock (the SIM2 time-warp).

Every engine timer reads time through THIS module instead of `time` directly,
so the whole system can be run faster than wall-clock without distorting a
single proportion. The feed's market evolution, the analytics/exit cadences,
cooldowns, holds, stall-kills, the 60s post-exit watch, gamma dwell, heart
windows, indicator look-backs — all scale together, because they all read the
same warped clock.

At speed 1.0 (LIVE, ordinary SIM, and the test-suite) every function is exactly
the stdlib `time.*` call — a guaranteed no-op, zero behaviour change. SIM2 sets
speed to e.g. 10.0: virtual time then advances 10× real time and `sleep(x)`
sleeps x/10 real seconds, so a session that would take hours plays out in
minutes with identical dynamics.

The asyncio server (uvicorn/websocket) deliberately does NOT use this clock — it
stays on real wall-clock so the UI socket and OS scheduling are unaffected.
`datetime.now()` is also untouched (it reads the OS clock at C level), so
human-readable timestamps remain real.
"""

import time as _time

_speed = 1.0
_mono_base_real = _time.monotonic()
_mono_base_virt = _mono_base_real
_wall_base_real = _time.time()
_wall_base_virt = _wall_base_real


def set_speed(s: float) -> None:
    """Set the time-warp factor. Rebases so virtual time is CONTINUOUS across
    the change (no jump). Call once, before the engine threads start."""
    global _speed, _mono_base_real, _mono_base_virt, _wall_base_real, _wall_base_virt
    _mono_base_virt = mono()              # freeze current virtual instants (old speed)
    _wall_base_virt = now()
    _mono_base_real = _time.monotonic()   # …against the current real instants
    _wall_base_real = _time.time()
    _speed = float(s)


def speed() -> float:
    return _speed


def mono() -> float:
    """Virtual monotonic seconds (for elapsed-time gates)."""
    return _mono_base_virt + (_time.monotonic() - _mono_base_real) * _speed


def now() -> float:
    """Virtual epoch seconds (for cooldowns / event freshness)."""
    return _wall_base_virt + (_time.time() - _wall_base_real) * _speed


def sleep(seconds: float) -> None:
    """Sleep `seconds` of VIRTUAL time — i.e. seconds/speed of real time."""
    if seconds <= 0:
        return
    _time.sleep(seconds / _speed)
