"""Configuration loading.

One YAML file holds the whole service.  On a Pi it lives on the **boot
partition** (``/boot/firmware/focussensor.yaml``) so it can be edited by
plugging the SD card into a laptop, without booting the Pi or finding an SSH
session — the same trick the SolarScope image uses.

``save()`` writes the current runtime parameters back, which is what
``POST /api/params/save`` does: tune the ROI and thresholds live over REST,
then persist them so they survive a reboot.
"""

import copy
import logging
import os
import threading
from typing import Any, Dict, Optional

log = logging.getLogger("focussensor.config")

DEFAULT_PATHS = (
    "/boot/firmware/focussensor.yaml",
    "/boot/focussensor.yaml",
    "config/focussensor.yaml",
)

DEFAULTS: Dict[str, Any] = {
    "name": "openuc2-focussensor",
    "server": {
        "host": "0.0.0.0",
        "port": 8321,
        "log_level": "info",
    },
    "camera": {
        # "auto" = real Pi camera when picamera2 imports, simulator otherwise.
        "backend": "auto",
        "camera_num": 0,
        # null ROI = the whole sensor. A fresh sensor has to show everything
        # before anyone can decide which narrow band the spots live in.
        "startup": {
            "exposure_us": 5000,
            "gain": 1.0,
            "binning": 1,
            "fps_target": 100.0,
            "roi": None,
        },
        "simulation": {
            "sensitivity_px_per_um": 3.0,
            "spot_separation_px": 220.0,
            "capture_range_um": 25.0,
            "drift_um_per_s": 0.02,
            "jitter_um_rms": 0.01,
        },
    },
    "focus": {
        "projection_mode": "max",
        "baseline_percentile": 20.0,
        "gaussian_sigma": 3.0,
        "peak_distance": 40,
        "peak_prominence_mad": 4.0,
        "peak_height_mad": 3.0,
        "history_length": 5,
        "min_quality": 0.0,
    },
    "stream": {
        "history_length": 2000,
        "mjpeg_fps": 10.0,
        "jpeg_quality": 80,
        "preview_max_width": 800,
    },
    # Write camera and estimator changes back to this file so a tuned sensor
    # comes back the same way after a power cycle.
    "persist_params": True,
    "persist_delay_s": 3.0,
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config(path: Optional[str] = None) -> Optional[str]:
    if path:
        return path if os.path.exists(path) else None
    for candidate in DEFAULT_PATHS:
        if os.path.exists(candidate):
            return candidate
    return None


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Merge the YAML file (if any) onto ``DEFAULTS``.

    A missing or unreadable file is not fatal: the defaults are a working
    simulated sensor, which is exactly what you want a fresh image to boot into.
    """
    resolved = find_config(path)
    if not resolved:
        log.info("no config file found, using built-in defaults")
        config = copy.deepcopy(DEFAULTS)
        config["_path"] = None
        return config

    try:
        import yaml
        with open(resolved, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except Exception:
        log.warning("could not read %s, using defaults", resolved, exc_info=True)
        loaded = {}

    config = _deep_merge(DEFAULTS, loaded)
    config["_path"] = resolved
    log.info("loaded config from %s", resolved)
    return config


def save_config(config: Dict[str, Any], path: Optional[str] = None) -> str:
    """Write the config back to disk. Returns the path written."""
    import yaml

    target = path or config.get("_path") or DEFAULT_PATHS[-1]
    payload = {k: v for k, v in config.items() if not k.startswith("_")}
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
    os.replace(tmp, target)     # atomic: never leave a half-written config
    log.info("saved config to %s", target)
    return target


class ParamStore:
    """Writes runtime parameter changes back to the config file.

    A sensor is tuned by moving sliders, not by editing YAML: exposure, gain,
    the ROI and the estimator thresholds all get adjusted over the API while
    watching the live view. Those values are what the sensor should come back
    with after a power cycle, so they are written back to the same file the
    defaults came from -- one file, no precedence puzzle between "the config"
    and "the saved state".

    Writes are debounced: dragging a slider produces a burst of changes and a
    boot partition is FAT on an SD card, so the file is rewritten once a few
    seconds after the last change rather than on every one. ``save_config``
    replaces atomically, so an interrupted write cannot leave a half-file.
    """

    def __init__(self, config: Dict[str, Any], snapshot, delay_s: Optional[float] = None,
                 enabled: Optional[bool] = None):
        self._config = config
        self._snapshot = snapshot          # callable -> dict merged into config
        self._delay = float(config.get("persist_delay_s", 3.0)
                            if delay_s is None else delay_s)
        self._enabled = bool(config.get("persist_params", True)
                             if enabled is None else enabled)
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self.last_saved_path: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def touch(self) -> None:
        """Note that something changed; write it out once things settle."""
        if not self._enabled:
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._write)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> Optional[str]:
        """Write now, cancelling any pending debounce. Returns the path."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        return self._write()

    def close(self) -> None:
        """Persist anything still pending, then stop."""
        with self._lock:
            pending = self._timer is not None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if pending and self._enabled:
            self._write()

    def _write(self) -> Optional[str]:
        try:
            self._config.update(self._snapshot())
            self.last_saved_path = save_config(self._config)
            self.last_error = None
            log.debug("persisted parameters to %s", self.last_saved_path)
            return self.last_saved_path
        except Exception as exc:                       # noqa: BLE001
            # Losing a settings write must never take the sensor down with it:
            # a read-only boot partition is annoying, not fatal.
            self.last_error = str(exc)
            log.warning("could not persist parameters: %s", exc)
            return None
