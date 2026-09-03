"""Camera backends for the focus sensor.

``create_camera()`` is the only thing the rest of the service imports: give it
the ``camera`` section of the config and it hands back something implementing
``CameraBase``.
"""

import logging
from typing import Any, Dict, Optional

from .base import CameraBase, CameraLimits, FrameMeta
from .simulated import SimParams, SimulatedTwoSpotCamera

log = logging.getLogger("focussensor.cameras")

__all__ = ["CameraBase", "CameraLimits", "FrameMeta", "SimParams",
           "SimulatedTwoSpotCamera", "create_camera", "picamera2_available"]


def picamera2_available() -> bool:
    try:
        import picamera2  # noqa: F401
        return True
    except Exception:
        return False


def create_camera(config: Optional[Dict[str, Any]] = None) -> CameraBase:
    """Build the camera named by ``config["backend"]``.

    ``"auto"`` picks the Pi camera when picamera2 imports and falls back to the
    simulator otherwise, so the same config file runs on a laptop and on a Pi.
    ``"picamera2"`` is explicit and fails loudly — on real hardware a silent
    fall back to synthetic pixels would be far worse than a crash, because a
    focus lock would happily run against a simulation.
    """
    config = dict(config or {})
    backend = str(config.pop("backend", "auto")).lower()

    limits_cfg = config.pop("limits", None)
    limits = CameraLimits(**limits_cfg) if limits_cfg else None
    sim_cfg = config.pop("simulation", None) or {}
    startup = config.pop("startup", None) or {}

    if backend in ("auto", "picamera2", "pi"):
        if backend != "auto" or picamera2_available():
            try:
                from .picamera2_backend import Picamera2Camera
                camera = Picamera2Camera(
                    camera_num=int(config.pop("camera_num", 0)),
                    limits=limits,
                    tuning_file=config.pop("tuning_file", None),
                )
                log.info("using %s", camera.model)
                return _apply_startup(camera, startup)
            except Exception:
                if backend != "auto":
                    raise
                log.warning("picamera2 unavailable, falling back to the simulator",
                            exc_info=True)

    camera = SimulatedTwoSpotCamera(limits=limits, params=SimParams(**sim_cfg))
    log.info("using %s", camera.model)
    return _apply_startup(camera, startup)


def _apply_startup(camera: CameraBase, startup: Dict[str, Any]) -> CameraBase:
    """Apply the config file's initial exposure/gain/ROI to a fresh camera."""
    if startup:
        camera.set_params(**startup)
    return camera
