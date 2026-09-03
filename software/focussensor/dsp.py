"""Small pure-numpy DSP kernels.

The focus estimator needs exactly two things from SciPy: ``gaussian_filter1d``
and ``find_peaks``.  Both are re-implemented here so the sensor runs on a bare
Raspberry Pi OS image with nothing but numpy installed — SciPy is a ~90 MB
dependency and a slow first import on a Pi Zero 2 W, for two functions that are
forty lines each.

The implementations follow SciPy's semantics closely enough that a projection
processed here and the same projection processed by
``imswitch.imcontrol.controller.focusmetrics`` land on the same peaks:

* ``gaussian_filter1d`` uses the same ``truncate=4.0`` kernel radius and the
  same edge handling (SciPy's ``mode="reflect"`` is numpy's ``"symmetric"`` —
  the sample at the border is repeated).
* ``find_peaks`` implements the same three filters in the same order
  (height, prominence, then distance keeping the tallest) and the same
  prominence definition (walk out to the first higher sample on each side).
"""

from typing import Dict, Optional, Tuple

import numpy as np

__all__ = ["gaussian_filter1d", "find_peaks", "parabolic_subpixel", "mad"]


def gaussian_filter1d(x: np.ndarray, sigma: float, truncate: float = 4.0) -> np.ndarray:
    """1-D Gaussian smoothing, equivalent to ``scipy.ndimage.gaussian_filter1d``."""
    x = np.asarray(x, dtype=np.float32)
    if sigma is None or sigma <= 0:
        return x
    radius = int(truncate * float(sigma) + 0.5)
    if radius < 1:
        return x
    t = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-0.5 * (t / float(sigma)) ** 2)
    kernel /= kernel.sum()
    # SciPy's "reflect" == numpy's "symmetric" (edge sample duplicated).
    padded = np.pad(x, radius, mode="symmetric")
    return np.convolve(padded, kernel, mode="valid").astype(np.float32)


def _local_maxima(x: np.ndarray) -> np.ndarray:
    """Indices of local maxima, plateau-aware (returns the plateau midpoint)."""
    n = x.size
    if n < 3:
        return np.empty(0, dtype=np.intp)

    peaks = []
    i = 1
    while i < n - 1:
        if x[i - 1] < x[i]:
            # Walk across a possible flat top.
            j = i
            while j < n - 1 and x[j + 1] == x[i]:
                j += 1
            if j < n - 1 and x[j + 1] < x[i]:
                peaks.append((i + j) // 2)
            i = j + 1
        else:
            i += 1
    return np.asarray(peaks, dtype=np.intp)


def _prominences(x: np.ndarray, peaks: np.ndarray) -> np.ndarray:
    """Topographic prominence of each peak (SciPy's definition, full window)."""
    out = np.empty(peaks.size, dtype=np.float32)
    n = x.size
    for k, p in enumerate(peaks):
        height = x[p]

        i = p
        left_min = height
        while i > 0 and x[i] <= height:
            i -= 1
            if x[i] < left_min:
                left_min = x[i]

        i = p
        right_min = height
        while i < n - 1 and x[i] <= height:
            i += 1
            if x[i] < right_min:
                right_min = x[i]

        out[k] = height - max(left_min, right_min)
    return out


def find_peaks(
    x: np.ndarray,
    height: Optional[float] = None,
    prominence: Optional[float] = None,
    distance: Optional[int] = None,
) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """Subset of ``scipy.signal.find_peaks`` — height, prominence and distance.

    Returns ``(peak_indices, properties)`` where properties carries
    ``peak_heights`` and ``prominences``, matching the SciPy keys the focus
    estimator reads.
    """
    x = np.asarray(x, dtype=np.float32)
    peaks = _local_maxima(x)
    if peaks.size == 0:
        return peaks, {"peak_heights": np.empty(0, np.float32),
                       "prominences": np.empty(0, np.float32)}

    heights = x[peaks]
    if height is not None:
        keep = heights >= float(height)
        peaks, heights = peaks[keep], heights[keep]
        if peaks.size == 0:
            return peaks, {"peak_heights": heights,
                           "prominences": np.empty(0, np.float32)}

    proms = _prominences(x, peaks)
    if prominence is not None:
        keep = proms >= float(prominence)
        peaks, heights, proms = peaks[keep], heights[keep], proms[keep]
        if peaks.size == 0:
            return peaks, {"peak_heights": heights, "prominences": proms}

    if distance is not None and distance > 1 and peaks.size > 1:
        # Tallest peak wins; anything closer than `distance` to a kept peak goes.
        order = np.argsort(heights)[::-1]
        keep = np.ones(peaks.size, dtype=bool)
        for idx in order:
            if not keep[idx]:
                continue
            too_close = np.abs(peaks - peaks[idx]) < int(distance)
            too_close[idx] = False
            keep &= ~too_close
        peaks, heights, proms = peaks[keep], heights[keep], proms[keep]

    return peaks, {"peak_heights": heights, "prominences": proms}


def parabolic_subpixel(y: np.ndarray, i: int) -> float:
    """3-point quadratic interpolation around index ``i`` for subpixel position."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    y0, y1, y2 = float(y[i - 1]), float(y[i]), float(y[i + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-9:
        return float(i)
    return float(i) + 0.5 * (y0 - y2) / denom


def mad(x: np.ndarray) -> float:
    """Median absolute deviation, with a floor so callers can divide by it."""
    med = np.median(x)
    return float(np.median(np.abs(x - med)) + 1e-9)
