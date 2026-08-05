"""Optional Numba-compiled kernel for the inner simulation block.

§4 policy: implement and benchmark the NumPy engine first, then move only the
proven hot kernel to compiled code, and only if the measured benefit justifies
the added complexity.

Profiling the NumPy engine showed ~70% of time inside the block body (allocating
and traversing (trials x cap+1) float arrays) and ~21% in searchsorted. Both are
memory-traffic bound rather than algorithmically slow, which is exactly the shape
where a fused scalar loop can win: it computes each trial's contribution in
registers without ever materialising the (trials x cap+1) intermediates.

If Numba is unavailable or fails to compile, `HAVE_NUMBA` is False and the caller
transparently uses the NumPy path. Both paths are covered by the same tests.
"""
from __future__ import annotations

import numpy as np

try:
    from numba import njit
    HAVE_NUMBA = True
except Exception:                                        # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):                                   # type: ignore
        def deco(f):
            return f
        return deco


@njit(cache=True, fastmath=True, nogil=True)
def _block_kernel(bids, counts, seats, cap, win_acc, sq_acc, paid_acc, beat_out):
    """Accumulate one block of trials.

    bids   : int32[total]   all rival bids in the block, grouped by trial
    counts : int64[n]       rivals per trial
    Writes into win_acc / sq_acc / paid_acc (float64[cap+1]) and beat_out (int32[n]).

    Everything is fused: one pass builds the per-trial histogram, a second walks
    bid levels downward carrying `above` so no (n x cap+1) array is ever created.
    """
    K = cap + 1
    n = counts.shape[0]
    hist = np.zeros(K, dtype=np.int64)
    pos = 0
    for t in range(n):
        c = counts[t]
        # Zero the WHOLE histogram. An earlier version cleared only the buckets
        # this trial touches, which left stale counts from previous trials in
        # every other bucket and silently corrupted the win curve. K is ~76, so
        # the full clear is negligible next to the two downward passes below.
        for b in range(K):
            hist[b] = 0
        for i in range(pos, pos + c):
            hist[bids[i]] += 1

        unfilled = (c + 1) <= seats
        above = 0
        cum = 0
        c1 = 0
        c2 = cap
        got1 = False
        got2 = seats - 1 <= 0
        # first downward pass: order statistics
        for b in range(K - 1, -1, -1):
            cum += hist[b]
            if (not got2) and seats - 1 > 0 and cum >= seats - 1:
                c2 = b
                got2 = True
            if (not got1) and cum >= seats:
                c1 = b
                got1 = True
            if got1 and got2:
                break
        if not got1:
            c1 = 0
        beat_out[t] = c1

        # second downward pass: win probability and price at every level
        above = 0
        for b in range(K - 1, -1, -1):
            eq = hist[b]
            room = seats - above
            if room <= 0:
                p = 0.0
            else:
                p = room / (eq + 1.0)
                if p > 1.0:
                    p = 1.0
            if p > 0.0:
                win_acc[b] += p
                sq_acc[b] += p * p
                if not unfilled:
                    price = b if b < c2 else c2
                    paid_acc[b] += p * price
            above += eq
        pos += c
    return 0


def kernel_available() -> bool:
    """True only if the kernel actually compiles and runs on this machine."""
    if not HAVE_NUMBA:
        return False
    try:
        bids = np.array([1, 2, 3], dtype=np.int32)
        counts = np.array([3], dtype=np.int64)
        w = np.zeros(4); s = np.zeros(4); p = np.zeros(4)
        b = np.zeros(1, dtype=np.int32)
        _block_kernel(bids, counts, 1, 3, w, s, p, b)
        return True
    except Exception:                                    # pragma: no cover
        return False
