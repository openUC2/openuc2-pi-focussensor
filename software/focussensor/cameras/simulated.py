"""Synthetic two-spot focus sensor.

Models the optics an off-axis reflection focus lock actually sees, so the
whole ImSwitch stack — live focus value, calibration sweep, PI lock — can be
exercised with no hardware at all.

The physics, deliberately simple but with the right *shape*:

* A collimated laser enters the objective off-axis and reflects off the two
  glass interfaces (coverslip bottom and top).  Each reflection lands on the
  sensor as a spot.
* Moving the sample in z shifts both spots along **x** by
  ``sensitivity_px_per_um * dz`` — the triangulation signal, linear over the
  capture range and gently compressed outside it (``tanh``), because a real
  spot walks off the detector rather than continuing forever.
* The two spots do not move quite together: their separation grows with defocus
  at ``separation_px_per_um``, since the two interfaces sit at different
  heights.  That reproduces the slow ``x_peak_distance`` drift a real system
  shows and gives the estimator's outlier logic something real to chew on.
* Spots blur with defocus (``sigma_growth_px_per_um``) and dim as they blur,
  conserving energy — so signal quality genuinely degrades away from focus and
  the lock has a finite capture range.
* Brightness scales with exposure and gain; noise is Poisson shot noise plus
  Gaussian read noise, both scaled correctly, and the result is clipped at the
  bit depth so overexposure looks like real overexposure.
* A slow thermal drift plus per-frame jitter run on a monotonic clock, so an
  idle lock has something to fight and a released lock visibly wanders off.

``z_um`` is driven from outside: ImSwitch mirrors its (virtual) stage position
in through ``/api/sim/state`` before each read, so a calibration sweep in
ImSwitch produces a real focus-vs-z curve here.
"""

import math
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .base import CameraBase, CameraLimits, FrameMeta


@dataclass
class SimParams:
    """Optical model + noise model. All of it is settable at runtime."""

    # --- geometry -----------------------------------------------------------
    z_focus_um: float = 0.0
    """Stage z at which the sensor sits at its nominal reading."""

    left_spot_x_px: Optional[float] = None
    """x of the left spot at ``z_focus_um``, in full-sensor pixels.
    ``None`` centres the pair on the sensor, which is where a centred ROI can
    actually see it."""

    spot_separation_px: float = 220.0
    """Distance between the two spots at ``z_focus_um``."""

    sensitivity_px_per_um: float = 3.0
    """Triangulation gain: spot travel in x per micron of defocus.
    This is the number a calibration sweep is supposed to recover."""

    separation_px_per_um: float = 0.12
    """How much the two spots spread apart per micron of defocus."""

    capture_range_um: float = 25.0
    """Half-width of the linear region; beyond it the response saturates."""

    spot_y_px: float = 0.0
    """y of both spots. 0 means "centre of the sensor"."""

    spot_tilt_px_per_um: float = 0.05
    """Small y drift with z — real setups are never perfectly aligned."""

    # --- spot shape ---------------------------------------------------------
    sigma_x_px: float = 5.0
    sigma_y_px: float = 7.0
    sigma_growth_px_per_um: float = 0.08
    """Defocus blur. Also dims the spot, since total energy is conserved."""

    left_amplitude: float = 190.0
    """Peak counts of the left (stronger) spot at reference exposure/gain."""

    right_relative_amplitude: float = 0.62
    """The second interface reflects less light."""

    # --- signal / noise -----------------------------------------------------
    reference_exposure_us: int = 5_000
    """Exposure at which the amplitudes above are reached with gain 1."""

    background_counts: float = 6.0
    read_noise_counts: float = 2.0
    shot_noise: bool = True
    exact_poisson: bool = False
    """Sample true Poisson statistics instead of the Gaussian approximation.
    Correct at very low signal, but several times slower over a full frame."""
    stray_light_gradient: float = 3.0
    """Counts of linear background ramp across x — the estimator's baseline
    removal has to cope with this."""

    hot_pixel_fraction: float = 0.0
    """Fraction of pixels stuck at saturation. Off by default; turn it up to
    check how well the estimator's smoothing rejects single-pixel spikes,
    which a max-projection is otherwise very exposed to."""

    # --- dynamics -----------------------------------------------------------
    drift_um_per_s: float = 0.02
    """Slow thermal drift added to the commanded z."""

    jitter_um_rms: float = 0.01
    """Per-frame z jitter (vibration)."""

    dropout_probability: float = 0.0
    """Chance of a frame with no spots at all — tests the estimator's
    ``valid=False`` path and ImSwitch's staleness handling."""

    seed: Optional[int] = 12345

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SimulatedTwoSpotCamera(CameraBase):
    """A ``CameraBase`` that renders the two-spot model above."""

    model = "simulated-twospot"
    simulated = True

    def __init__(self, limits: Optional[CameraLimits] = None,
                 params: Optional[SimParams] = None):
        super().__init__(limits or CameraLimits(
            full_width=1456, full_height=1088, bit_depth=8, fps_max=200.0))
        self.params = params or SimParams()
        self._rng = np.random.default_rng(self.params.seed)
        self._z_um = self.params.z_focus_um
        self._z_lock = threading.Lock()
        self._t0 = time.monotonic()
        self._next_frame_at = 0.0
        # Starts full-frame, like the real camera: the ROI is something you
        # narrow down once you can see where the spots actually land.

    # ------------------------------------------------------------- simulation
    @property
    def z_um(self) -> float:
        """Commanded stage position (without drift/jitter)."""
        with self._z_lock:
            return self._z_um

    def set_z(self, z_um: float) -> float:
        with self._z_lock:
            self._z_um = float(z_um)
            return self._z_um

    def move_z(self, dz_um: float) -> float:
        with self._z_lock:
            self._z_um += float(dz_um)
            return self._z_um

    def update_params(self, **kwargs) -> SimParams:
        for key, value in kwargs.items():
            if not hasattr(self.params, key):
                raise KeyError(f"unknown simulation parameter '{key}'")
            setattr(self.params, key, value)
        if "seed" in kwargs:
            self._rng = np.random.default_rng(self.params.seed)
        return self.params

    def _effective_z(self) -> float:
        """Commanded z plus drift and vibration — what the optics really see."""
        p = self.params
        elapsed = time.monotonic() - self._t0
        z = self.z_um + p.drift_um_per_s * elapsed
        if p.jitter_um_rms > 0:
            z += float(self._rng.normal(0.0, p.jitter_um_rms))
        return z

    def _spot_geometry(self, dz: float) -> Tuple[float, float, float, float, float]:
        """(x_left, x_right, y, sigma_x, dim_factor) for a given defocus."""
        p = self.params
        # Linear near focus, saturating far away: a spot cannot travel forever.
        r = max(1e-6, p.capture_range_um)
        travel = r * math.tanh(dz / r) * p.sensitivity_px_per_um

        separation = p.spot_separation_px + p.separation_px_per_um * dz
        base_x = p.left_spot_x_px
        if base_x is None:
            base_x = (self.full_shape[0] - p.spot_separation_px) / 2.0
        x_left = base_x + travel
        x_right = x_left + separation

        fh = self.full_shape[1]
        y = (p.spot_y_px or fh / 2.0) + p.spot_tilt_px_per_um * dz

        sigma_x = p.sigma_x_px + p.sigma_growth_px_per_um * abs(dz)
        # Energy conservation: a wider spot has a lower peak.
        dim = (p.sigma_x_px / sigma_x) if sigma_x > 0 else 1.0
        return x_left, x_right, y, sigma_x, dim

    # ------------------------------------------------------------------ render
    def _render(self) -> Tuple[np.ndarray, float]:
        p = self.params
        x0, y0, w, h = self._roi
        z = self._effective_z()
        dz = z - p.z_focus_um

        # Work in ROI coordinates; the model is defined in full-sensor pixels.
        xs = np.arange(x0, x0 + w, dtype=np.float32)
        ys = np.arange(y0, y0 + h, dtype=np.float32)

        img = np.full((h, w), p.background_counts, dtype=np.float32)
        if p.stray_light_gradient:
            img += np.linspace(0.0, p.stray_light_gradient, w, dtype=np.float32)[None, :]

        drop = p.dropout_probability > 0 and self._rng.random() < p.dropout_probability
        if not drop:
            x_left, x_right, y_c, sigma_x, dim = self._spot_geometry(dz)
            sigma_y = p.sigma_y_px + p.sigma_growth_px_per_um * abs(dz)

            # Exposure/gain scaling relative to the reference operating point.
            scale = (self._exposure_us / max(1, p.reference_exposure_us)) * self._gain * dim
            gy = np.exp(-0.5 * ((ys - y_c) / sigma_y) ** 2)
            for x_c, amp in ((x_left, p.left_amplitude),
                             (x_right, p.left_amplitude * p.right_relative_amplitude)):
                gx = np.exp(-0.5 * ((xs - x_c) / sigma_x) ** 2)
                img += (amp * scale) * np.outer(gy, gx)

        # Shot noise plus read noise. Poisson and Gaussian noise add in
        # variance, so both come from a single normal draw with
        # sigma = sqrt(signal + read^2) -- about four times cheaper than
        # sampling Poisson over a full 1.6 Mpx frame, and indistinguishable
        # above a handful of counts. Set `exact_poisson` when the discreteness
        # at very low signal actually matters.
        if p.exact_poisson:
            if p.shot_noise:
                img = self._rng.poisson(np.clip(img, 0, None)).astype(np.float32)
            if p.read_noise_counts > 0:
                img += self._rng.normal(0.0, p.read_noise_counts,
                                        img.shape).astype(np.float32)
        else:
            variance = np.clip(img, 0, None) if p.shot_noise else np.zeros_like(img)
            if p.read_noise_counts > 0:
                variance = variance + p.read_noise_counts ** 2
            if variance.any():
                img = img + np.sqrt(variance) * self._rng.standard_normal(
                    img.shape, dtype=np.float32)
        if p.hot_pixel_fraction > 0:
            n_hot = int(p.hot_pixel_fraction * img.size)
            if n_hot:
                idx = self._rng.integers(0, img.size, n_hot)
                img.flat[idx] = self.saturation_value

        img = np.clip(img, 0.0, self.saturation_value)
        if self._binning > 1:
            b = self._binning
            img = img[: h - h % b, : w - w % b]
            img = img.reshape(img.shape[0] // b, b, img.shape[1] // b, b).mean(axis=(1, 3))

        dtype = np.uint8 if self._limits.bit_depth <= 8 else np.uint16
        return img.astype(dtype), z

    # ------------------------------------------------------------------- grab
    def _grab(self) -> Tuple[np.ndarray, FrameMeta]:
        # Pace the loop to the achievable frame rate so timing looks like a
        # real sensor and the engine's fps accounting is meaningful.
        period = 1.0 / max(1e-3, self.achievable_fps)
        now = time.monotonic()
        if now < self._next_frame_at:
            time.sleep(self._next_frame_at - now)
            now = time.monotonic()
        self._next_frame_at = max(now, self._next_frame_at + period)

        with self._lock:
            frame, z = self._render()
        return frame, FrameMeta(t=time.time(), t_mono=time.monotonic(), z_um=z)

    def get_params(self) -> Dict[str, Any]:
        params = super().get_params()
        params["sim"] = {"z_um": self.z_um, "effective_z_um": self._effective_z()}
        return params
