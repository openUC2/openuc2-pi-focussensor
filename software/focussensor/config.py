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
        "startup": {
            "exposure_us": 5000,
            "gain": 1.0,
            "binning": 1,
            "fps_target": 100.0,
            "roi": {"x": 408, "y": 444, "width": 640, "height": 200},
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
    },
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
