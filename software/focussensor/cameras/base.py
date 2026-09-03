"""Camera interface shared by the simulated and the Picamera2 backends.

Everything ImSwitch (or the standalone client) can change about acquisition
lives here, so a backend is a matter of implementing ``_apply_*`` and
``_grab``.  The property surface deliberately mirrors what ImSwitch's
``DetectorManager`` exposes — exposure, gain, ROI, binning — plus the frame
rate cap that matters for a sensor that is supposed to run fast.

Coordinate convention: ROI is given in **full-sensor pixels** as
``(x, y, width, height)`` with the origin top-left, exactly like
``DetectorManager.crop(hpos, vpos, hsize, vsize)``.  Delivered frames are ROI
sized, and after binning are ``(height // binning, width // binning)``.
"""

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np


@dataclass
class CameraLimits:
    """What the backend will actually accept — served at ``/api/camera/limits``
    so a UI can build sliders without hardcoding hardware knowledge."""

    exposure_us_min: int = 50
    exposure_us_max: int = 200_000
    gain_min: float = 1.0
    gain_max: float = 16.0
    fps_max: float = 200.0
    supported_binnings: Tuple[int, ...] = (1, 2, 4)
    bit_depth: int = 8
    full_width: int = 640
    full_height: int = 480

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["supported_binnings"] = list(self.supported_binnings)
        return d


@dataclass
class FrameMeta:
    """Per-frame bookkeeping travelling next to the pixels."""

    seq: int = 0
    t: float = 0.0          # wall clock, seconds since epoch
    t_mono: float = 0.0     # monotonic seconds
    exposure_us: int = 0
    gain: float = 1.0
    binning: int = 1
    roi: Tuple[int, int, int, int] = (0, 0, 0, 0)
    z_um: Optional[float] = None   # simulated backends only


class CameraBase(ABC):
    """Base class for focus-sensor cameras."""

    #: Human-readable backend name, reported at ``/api/status``.
    model = "base"

    #: True when the pixels are synthetic — the server exposes the ``/api/sim``
    #: endpoints only then, and ImSwitch uses it to decide whether to mirror
    #: the stage position into the sensor.
    simulated = False

    def __init__(self, limits: Optional[CameraLimits] = None):
        self._limits = limits or CameraLimits()
        self._lock = threading.RLock()
        self._running = False
        self._seq = 0

        self._exposure_us = 5_000
        self._gain = 1.0
        self._binning = 1
        self._fps_target = 60.0
        self._roi = (0, 0, self._limits.full_width, self._limits.full_height)

    # ----------------------------------------------------------------- lifecyle
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._on_start()
            self._running = True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._on_stop()
            self._running = False

    def close(self) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._running

    def _on_start(self) -> None:
        """Backend hook; default is a no-op."""

    def _on_stop(self) -> None:
        """Backend hook; default is a no-op."""

    # ------------------------------------------------------------------ capture
    def grab(self) -> Tuple[np.ndarray, FrameMeta]:
        """Return the next ROI frame plus its metadata.

        Blocks until a frame is available. Backends implement ``_grab``.
        """
        if not self._running:
            raise RuntimeError("camera is not running")
        frame, meta = self._grab()
        self._seq += 1
        meta.seq = self._seq
        meta.exposure_us = self._exposure_us
        meta.gain = self._gain
        meta.binning = self._binning
        meta.roi = self._roi
        return frame, meta

    @abstractmethod
    def _grab(self) -> Tuple[np.ndarray, FrameMeta]:
        ...

    # --------------------------------------------------------------- properties
    @property
    def limits(self) -> CameraLimits:
        return self._limits

    @property
    def full_shape(self) -> Tuple[int, int]:
        """``(width, height)`` of the whole sensor."""
        return (self._limits.full_width, self._limits.full_height)

    @property
    def shape(self) -> Tuple[int, int]:
        """``(width, height)`` of a delivered frame, after ROI and binning."""
        _, _, w, h = self._roi
        return (w // self._binning, h // self._binning)

    @property
    def saturation_value(self) -> float:
        return float((1 << self._limits.bit_depth) - 1)

    # exposure ---------------------------------------------------------------
    @property
    def exposure_us(self) -> int:
        return self._exposure_us

    @exposure_us.setter
    def exposure_us(self, value: float) -> None:
        value = int(round(float(value)))
        value = max(self._limits.exposure_us_min, min(self._limits.exposure_us_max, value))
        with self._lock:
            self._exposure_us = value
            self._apply_exposure(value)

    def _apply_exposure(self, exposure_us: int) -> None:
        """Backend hook."""

    # gain -------------------------------------------------------------------
    @property
    def gain(self) -> float:
        return self._gain

    @gain.setter
    def gain(self, value: float) -> None:
        value = max(self._limits.gain_min, min(self._limits.gain_max, float(value)))
        with self._lock:
            self._gain = value
            self._apply_gain(value)

    def _apply_gain(self, gain: float) -> None:
        """Backend hook."""

    # binning ----------------------------------------------------------------
    @property
    def binning(self) -> int:
        return self._binning

    @binning.setter
    def binning(self, value: int) -> None:
        value = int(value)
        if value not in self._limits.supported_binnings:
            raise ValueError(
                f"binning {value} not supported (have {list(self._limits.supported_binnings)})")
        with self._lock:
            self._binning = value
            self._apply_binning(value)

    def _apply_binning(self, binning: int) -> None:
        """Backend hook."""

    # frame rate -------------------------------------------------------------
    @property
    def fps_target(self) -> float:
        return self._fps_target

    @fps_target.setter
    def fps_target(self, value: float) -> None:
        value = max(0.1, min(self._limits.fps_max, float(value)))
        with self._lock:
            self._fps_target = value
            self._apply_fps(value)

    def _apply_fps(self, fps: float) -> None:
        """Backend hook."""

    @property
    def achievable_fps(self) -> float:
        """Frame rate the sensor can actually sustain at the current exposure."""
        exposure_limited = 1e6 / max(1, self._exposure_us)
        return min(self._fps_target, exposure_limited, self._limits.fps_max)

    # ROI --------------------------------------------------------------------
    @property
    def roi(self) -> Tuple[int, int, int, int]:
        return self._roi

    def set_roi(self, x: int, y: int, width: int, height: int) -> Tuple[int, int, int, int]:
        """Clamp the requested window to the sensor and apply it."""
        fw, fh = self.full_shape
        width = max(self._binning, min(int(width), fw))
        height = max(self._binning, min(int(height), fh))
        x = max(0, min(int(x), fw - width))
        y = max(0, min(int(y), fh - height))
        # Keep the window an exact multiple of the binning factor.
        width -= width % self._binning
        height -= height % self._binning
        with self._lock:
            self._roi = (x, y, width, height)
            self._apply_roi(self._roi)
        return self._roi

    def center_roi(self, width: int, height: int) -> Tuple[int, int, int, int]:
        """Convenience: a window of this size centred on the sensor."""
        fw, fh = self.full_shape
        return self.set_roi((fw - width) // 2, (fh - height) // 2, width, height)

    def _apply_roi(self, roi: Tuple[int, int, int, int]) -> None:
        """Backend hook."""

    # ------------------------------------------------------------ bulk get/set
    def get_params(self) -> Dict[str, Any]:
        x, y, w, h = self._roi
        return {
            "exposure_us": self._exposure_us,
            "gain": self._gain,
            "binning": self._binning,
            "fps_target": self._fps_target,
            "achievable_fps": round(self.achievable_fps, 2),
            "roi": {"x": x, "y": y, "width": w, "height": h},
            "frame_shape": {"width": self.shape[0], "height": self.shape[1]},
            "full_shape": {"width": self.full_shape[0], "height": self.full_shape[1]},
            "bit_depth": self._limits.bit_depth,
            "model": self.model,
            "simulated": self.simulated,
            "running": self._running,
        }

    def set_params(self, **kwargs) -> Dict[str, Any]:
        """Apply any subset of the writable parameters.

        Order matters: binning first (it constrains the ROI), then the ROI,
        then the scalars.
        """
        if "binning" in kwargs and kwargs["binning"] is not None:
            self.binning = kwargs["binning"]
        roi = kwargs.get("roi")
        if roi:
            if isinstance(roi, dict):
                self.set_roi(roi.get("x", self._roi[0]), roi.get("y", self._roi[1]),
                             roi.get("width", self._roi[2]), roi.get("height", self._roi[3]))
            else:
                self.set_roi(*roi)
        for key in ("exposure_us", "gain", "fps_target"):
            if kwargs.get(key) is not None:
                setattr(self, key, kwargs[key])
        return self.get_params()
