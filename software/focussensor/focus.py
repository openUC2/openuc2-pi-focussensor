"""Focus estimation from a two-spot reflection image.

The optical setup: a laser is sent into the objective off-axis, reflects off
the sample interfaces (typically coverslip top and bottom) and lands on the
sensor as **two spots**.  Defocus translates both spots along x — this is the
triangulation signal.  The focus value is the x position of the left spot; the
separation between the two spots is a useful secondary observable (it drifts
much more slowly and is a good health check).

This is a port of ``PeakMetric`` from
``imswitch/imcontrol/controller/focusmetrics.py`` — same pipeline, same output
keys — so that moving the computation onto the sensor does not change what
ImSwitch sees:

    x-projection -> baseline removal -> Gaussian smoothing -> MAD scaling
      -> peak finding -> two strongest peaks -> parabolic subpixel

Everything runs on ``float32`` over a single projection vector, so a 640x200
ROI costs well under a millisecond on a Pi Zero 2 W.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from . import dsp


@dataclass
class FocusParams:
    """Everything about the estimator that the REST API can change."""

    projection_mode: str = "max"
    """``"max"`` (bright dots on dark ground, the default) or ``"mean"``."""

    background_threshold: Optional[float] = None
    """Absolute offset subtracted from the projection. ``None`` uses
    ``baseline_percentile`` instead, which adapts to changing stray light."""

    baseline_percentile: float = 20.0
    """Percentile used as the baseline when ``background_threshold`` is None."""

    enable_gaussian_blur: bool = True
    gaussian_sigma: float = 3.0
    """Smoothing of the projection, in pixels. Roughly the spot radius."""

    peak_distance: int = 40
    """Minimum separation between the two peaks, in pixels."""

    peak_prominence_mad: float = 4.0
    """Peak prominence threshold, in units of the projection's noise MAD."""

    peak_height_mad: float = 3.0
    """Peak height threshold, in units of the projection's noise MAD."""

    peak_max_distance: Optional[int] = None
    """If the two candidates are further apart than this, keep only the
    strongest — guards against latching onto a stray reflection."""

    left_peak_roi: Optional[List[int]] = None
    """``[xmin, xmax]`` window the left spot must fall inside, in ROI pixels."""

    history_length: int = 5
    """Number of past separations kept for the outlier check."""

    outlier_threshold_mad: float = 6.0
    """Separation values this far (in MAD) from the running median are not
    admitted into the history."""

    min_quality: float = 0.0
    """Samples whose peak prominence (in MAD) is below this are reported with
    ``valid=False``. 0 disables the check."""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FocusSample:
    """One estimate. The wire format of both ``/api/focus`` and the WebSocket."""

    seq: int = 0
    t: float = 0.0
    """Wall-clock capture time, **seconds** since the epoch (float)."""

    t_mono: float = 0.0
    """Monotonic capture time in seconds — use this for rate/staleness math."""

    valid: bool = False
    focus: Optional[float] = None
    """The focus value = subpixel x of the left spot, in ROI pixels."""

    left_peak_x: Optional[float] = None
    right_peak_x: Optional[float] = None
    x_peak_distance: Optional[float] = None
    avg_peak_distance: Optional[float] = None
    n_peaks: int = 0
    quality: float = 0.0
    """Prominence of the left peak in noise-MAD units. Higher is better."""

    saturated_fraction: float = 0.0
    compute_ms: float = 0.0
    z_um: Optional[float] = None
    """Simulated stage position, when running the simulated camera."""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PeakFocusEstimator:
    """Two-spot peak estimator. Not thread-safe; the engine owns one."""

    def __init__(self, params: Optional[FocusParams] = None):
        self.params = params or FocusParams()
        self._separations: List[float] = []
        self._seq = 0

    # ------------------------------------------------------------------ params
    def update_params(self, **kwargs) -> FocusParams:
        for key, value in kwargs.items():
            if not hasattr(self.params, key):
                raise KeyError(f"unknown focus parameter '{key}'")
            setattr(self.params, key, value)
        return self.params

    def reset_history(self) -> None:
        self._separations = []

    # ------------------------------------------------------------- separations
    def _is_outlier(self, separation: float) -> bool:
        if len(self._separations) < 3:
            return False
        hist = np.asarray(self._separations, dtype=np.float32)
        return abs(separation - float(np.median(hist))) / dsp.mad(hist) > \
            self.params.outlier_threshold_mad

    def _push_separation(self, separation: float) -> None:
        self._separations.append(float(separation))
        while len(self._separations) > max(1, self.params.history_length):
            self._separations.pop(0)

    @property
    def average_separation(self) -> Optional[float]:
        return float(np.mean(self._separations)) if self._separations else None

    # ------------------------------------------------------------------ compute
    def compute(self, frame: np.ndarray, *, saturation_value: Optional[float] = None
                ) -> "tuple[FocusSample, np.ndarray]":
        """Estimate focus from one ROI frame.

        Returns the sample plus the smoothed projection, which the debug
        overlay draws and ``/api/focus/projection`` serves.
        """
        p = self.params
        t_start = time.monotonic()
        self._seq += 1

        im = np.asarray(frame)
        if im.ndim == 3:  # colour sensor: collapse to luma-ish
            im = im.mean(axis=2)
        im = im.astype(np.float32, copy=False)

        sample = FocusSample(seq=self._seq, t=time.time(), t_mono=t_start)
        if saturation_value:
            sample.saturated_fraction = float(np.count_nonzero(im >= saturation_value) / im.size)

        # 1) project along y -> one row of x
        proj = np.max(im, axis=0) if p.projection_mode == "max" else np.mean(im, axis=0)

        # 2) baseline removal
        if p.background_threshold is not None:
            proj = np.clip(proj - float(p.background_threshold), 0.0, None)
        else:
            proj = np.clip(proj - float(np.percentile(proj, p.baseline_percentile)), 0.0, None)

        # 3) smoothing
        proj_s = dsp.gaussian_filter1d(proj, p.gaussian_sigma) \
            if p.enable_gaussian_blur else proj.astype(np.float32)

        # 4) rescale into noise units so the thresholds are exposure-independent
        noise = dsp.mad(proj_s)
        zsig = np.clip((proj_s - float(np.median(proj_s))) / noise, 0.0, None)

        # 5) peaks
        peaks, props = dsp.find_peaks(
            zsig,
            height=p.peak_height_mad,
            prominence=p.peak_prominence_mad,
            distance=int(p.peak_distance),
        )

        if p.left_peak_roi and peaks.size:
            xmin, xmax = p.left_peak_roi
            keep = (peaks >= xmin) & (peaks <= xmax)
            peaks = peaks[keep]
            props = {k: np.asarray(v)[keep] for k, v in props.items()}

        sample.n_peaks = int(peaks.size)

        if peaks.size:
            proms = props.get("prominences", zsig[peaks])
            order = np.argsort(proms)
            best = np.sort(peaks[order][-2:] if peaks.size >= 2 else peaks[order][-1:])

            if p.peak_max_distance is not None and best.size == 2 and \
                    (best[1] - best[0]) > int(p.peak_max_distance):
                best = best[-1:]

            left_i = int(best[0])
            sample.left_peak_x = dsp.parabolic_subpixel(proj_s, left_i)
            sample.quality = float(proms[np.where(peaks == left_i)[0][0]]) if peaks.size else 0.0

            if best.size == 2:
                sample.right_peak_x = dsp.parabolic_subpixel(proj_s, int(best[1]))
                separation = float(sample.right_peak_x - sample.left_peak_x)
                sample.x_peak_distance = separation
                if not self._is_outlier(separation):
                    self._push_separation(separation)

            sample.focus = sample.left_peak_x
            sample.valid = sample.quality >= p.min_quality

        sample.avg_peak_distance = self.average_separation
        sample.compute_ms = (time.monotonic() - t_start) * 1000.0
        return sample, proj_s
