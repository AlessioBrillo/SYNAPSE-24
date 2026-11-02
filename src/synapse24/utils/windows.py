"""Analysis windows over a recording: one rule for the export and the worker.

The export (XDF -> FIT) and the worker (``hrv_trends`` on an XDF) used to count windows
differently (``ceil`` in one, ``floor`` in the other), so the same recording yielded a
different number of windows depending on which path read it.
"""

from __future__ import annotations

import math

# A duration that is an exact multiple of the window must not gain a window to
# floating-point noise (600.0000000001 s / 300 s).
_EPS = 1e-9


def window_bounds(t0: float, t1: float, window_s: float) -> list[tuple[float, float]]:
    """Consecutive windows covering ``[t0, t1]``; the last one is clipped to ``t1``.

    There is always at least one window, so a recording shorter than ``window_s`` is
    still analysed (as one clipped window) instead of silently producing nothing.
    """
    if window_s <= 0:
        raise ValueError(f"window_s must be positive, got {window_s}")
    if t1 < t0:
        raise ValueError(f"t1 ({t1}) is before t0 ({t0})")
    count = max(1, math.ceil((t1 - t0) / window_s - _EPS))
    return [(t0 + i * window_s, min(t0 + (i + 1) * window_s, t1)) for i in range(count)]
