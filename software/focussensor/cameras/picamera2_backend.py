"""Raspberry Pi camera backend (libcamera / picamera2).

Implements the same ``CameraBase`` contract as the simulator, so nothing above
this file changes when the simulation is swapped for real optics.
``create_camera({"backend": "auto"})`` picks this automatically whenever
picamera2 imports, which on a Pi means it comes up on its own at boot.

Three things make this a *measuring* camera rather than a picture camera, and
all three matter to a focus lock:

**Everything automatic is off.** AE, AWB, denoise, sharpening and any tone
curve are disabled and pinned to neutral. An auto-exposure loop hunting
between frames would show up directly as focus noise, and a denoiser would
move the spot centroid. Exposure and gain do exactly what you set.

**The output is grayscale.** A mono sensor is read as ``R8``; a colour sensor
falls back to ``YUV420`` and only the luma plane is used, which is the cheapest
thing the ISP can hand over. Either way the estimator sees one channel, so
white balance is irrelevant — it is fixed at unity purely so the debug JPEG
does not develop a colour cast.

**Requested is not achieved.** libcamera silently clamps exposure to fit the
frame duration and gain to the sensor's real steps. ``hardware_params()``
reports what the sensor says it actually did, read back from frame metadata,
so a clamped exposure is visible instead of mysterious.

The camera starts on the **full sensor**. A narrow ROI is what makes this fast,
but you cannot choose one until you can see where the spots land, so narrowing
is a deliberate step rather than a default you have to discover.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .base import CameraBase, CameraLimits, FrameMeta

log = logging.getLogger("focussensor.cameras.picamera2")

# Sensor formats that carry a Bayer mosaic; anything else single-plane is mono.
_BAYER_PREFIXES = ("SRGGB", "SBGGR", "SGRBG", "SGBRG")


class Picamera2Camera(CameraBase):
    """Live Raspberry Pi camera through picamera2."""

    model = "picamera2"
    simulated = False

    def __init__(self, camera_num: int = 0, limits: Optional[CameraLimits] = None,
                 tuning_file: Optional[str] = None):
        from picamera2 import Picamera2  # imported lazily: absent off-Pi

        kwargs = {}
        if tuning_file:
            kwargs["tuning"] = Picamera2.load_tuning_file(tuning_file)
        self._picam2 = Picamera2(camera_num, **kwargs)

        props = self._picam2.camera_properties
        full_w, full_h = props.get("PixelArraySize", (1456, 1088))

        limits = limits or CameraLimits()
        limits.full_width, limits.full_height = int(full_w), int(full_h)

        # Ask the pipeline what it will actually accept rather than guessing.
        self._controls = self._picam2.camera_controls or {}
        if "ExposureTime" in self._controls:
            limits.exposure_us_min = int(self._controls["ExposureTime"][0])
            limits.exposure_us_max = int(self._controls["ExposureTime"][1])
        if "AnalogueGain" in self._controls:
            limits.gain_min = float(self._controls["AnalogueGain"][0])
            limits.gain_max = float(self._controls["AnalogueGain"][1])
        if "FrameDurationLimits" in self._controls:
            shortest_us = int(self._controls["FrameDurationLimits"][0])
            if shortest_us > 0:
                limits.fps_max = round(1e6 / shortest_us, 1)

        self._mono = self._detect_mono()
        # ScalerCrop is expressed in sensor-array coordinates; the pipeline
        # tells us the rectangle it will accept.
        crop_max = props.get("ScalerCropMaximum") or (0, 0, int(full_w), int(full_h))
        self._crop_max = tuple(int(v) for v in crop_max) if any(crop_max[2:]) \
            else (0, 0, int(full_w), int(full_h))

        self.model = (f"picamera2:{props.get('Model', 'unknown')}"
                      f"{'/mono' if self._mono else '/colour'}")
        super().__init__(limits)

        self._configured: Optional[Tuple[Tuple[int, int, int, int], int, str]] = None
        self._output_format = "R8" if self._mono else "YUV420"
        self._last_metadata: Dict[str, Any] = {}
        log.info("%s: %dx%d sensor, exposure %d-%d us, gain %.1f-%.1f, <= %.0f fps",
                 self.model, full_w, full_h, limits.exposure_us_min,
                 limits.exposure_us_max, limits.gain_min, limits.gain_max,
                 limits.fps_max)

    # ------------------------------------------------------------- detection
    def _detect_mono(self) -> bool:
        """True when the sensor has no colour filter array."""
        try:
            for mode in self._picam2.sensor_modes or []:
                fmt = str(mode.get("format", "") or mode.get("unpacked", ""))
                if fmt:
                    return not fmt.upper().startswith(_BAYER_PREFIXES)
        except Exception:
            log.debug("could not read sensor modes", exc_info=True)
        return False

    # ------------------------------------------------------------------ config
    def _manual_controls(self) -> Dict[str, Any]:
        """Every automatic algorithm off, exposure and gain exactly as asked.

        The dict is filtered against what this pipeline advertises: not every
        control exists on every sensor (``ColourGains`` on a mono camera, for
        one), and setting a control libcamera does not know about raises.
        """
        frame_us = int(1e6 / max(0.1, self._fps_target))
        # libcamera stretches the frame duration to fit a long exposure, which
        # silently drops the rate. Clamp instead, and report the difference.
        exposure = min(self._exposure_us,
                       max(self._limits.exposure_us_min, frame_us - 100))

        wanted = {
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": int(exposure),
            "AnalogueGain": float(self._gain),
            "FrameDurationLimits": (frame_us, frame_us),
            "NoiseReductionMode": 0,     # Off: a denoiser moves spot centroids
            "Sharpness": 0.0,            # no edge enhancement near a peak
            "Contrast": 1.0,             # neutral tone curve
            "Brightness": 0.0,
            "Saturation": 0.0,           # colour sensor -> neutral grey
            "ColourGains": (1.0, 1.0),   # fixed WB; only legal with AwbEnable off
            "AeFlickerMode": 0,
        }
        return {k: v for k, v in wanted.items() if k in self._controls}

    def _scaler_crop(self) -> Tuple[int, int, int, int]:
        """Our ROI clamped into the pipeline's allowed crop rectangle."""
        cx, cy, cw, ch = self._crop_max
        x, y, w, h = self._roi
        w = max(2, min(w, cw))
        h = max(2, min(h, ch))
        x = max(cx, min(x + cx, cx + cw - w))
        y = max(cy, min(y + cy, cy + ch - h))
        return (x, y, w, h)

    def _configure(self) -> None:
        """(Re)build the libcamera configuration for the current ROI/binning.

        A window or format change is a reconfiguration, not a control write, so
        it has to stop the camera. Exposure, gain and frame rate do not.
        """
        from picamera2 import Picamera2  # noqa: F401  (import guard for typing)

        x, y, w, h = self._roi
        out_w, out_h = w // self._binning, h // self._binning
        out_w -= out_w % 2                       # YUV420 wants even dimensions
        out_h -= out_h % 2
        out_w, out_h = max(2, out_w), max(2, out_h)

        was_running = self._picam2.started
        if was_running:
            self._picam2.stop()

        controls = self._manual_controls()
        controls["ScalerCrop"] = self._scaler_crop()

        for fmt in ([self._output_format] if self._output_format == "R8"
                    else []) + ["YUV420"]:
            try:
                config = self._picam2.create_video_configuration(
                    main={"size": (out_w, out_h), "format": fmt},
                    controls=controls,
                    buffer_count=4,
                    # Always hand back the newest frame, never a queued one: a
                    # control loop wants the present, not the recent past.
                    queue=False,
                )
                self._picam2.configure(config)
                self._output_format = fmt
                break
            except Exception:
                log.warning("format %s rejected, falling back", fmt, exc_info=True)
        else:
            raise RuntimeError("no usable output format (tried R8 and YUV420)")

        self._configured = (self._roi, self._binning, self._output_format)
        log.info("configured %s %dx%d from ROI %s", self._output_format,
                 out_w, out_h, self._roi)
        if was_running:
            self._picam2.start()
            self._picam2.set_controls(controls)

    # --------------------------------------------------------------- lifecycle
    def _on_start(self) -> None:
        if self._configured != (self._roi, self._binning, self._output_format):
            self._configure()
        if not self._picam2.started:
            self._picam2.start()
        controls = self._manual_controls()
        controls["ScalerCrop"] = self._scaler_crop()
        self._picam2.set_controls(controls)
        # Let the first frames flush through with the new controls applied;
        # libcamera takes a few frames to settle a manual exposure change.
        time.sleep(0.3)

    def _on_stop(self) -> None:
        if self._picam2.started:
            self._picam2.stop()

    def close(self) -> None:
        super().close()
        try:
            self._picam2.close()
        except Exception:
            log.debug("camera close failed", exc_info=True)

    # ----------------------------------------------------------------- setters
    def _push_controls(self) -> None:
        if self._picam2.started:
            try:
                self._picam2.set_controls(self._manual_controls())
            except Exception:
                log.warning("could not apply controls", exc_info=True)

    def _apply_exposure(self, exposure_us: int) -> None:
        self._push_controls()

    def _apply_gain(self, gain: float) -> None:
        self._push_controls()

    def _apply_fps(self, fps: float) -> None:
        self._push_controls()

    def _apply_roi(self, roi: Tuple[int, int, int, int]) -> None:
        if self._configured is not None:
            self._configure()

    def _apply_binning(self, binning: int) -> None:
        if self._configured is not None:
            self._configure()

    # -------------------------------------------------------------------- grab
    def _grab(self) -> Tuple[np.ndarray, FrameMeta]:
        request = self._picam2.capture_request()
        try:
            array = request.make_array("main")
            self._last_metadata = request.get_metadata() or {}
        finally:
            request.release()

        out_w, out_h = self.shape
        out_w -= out_w % 2
        out_h -= out_h % 2
        if self._output_format == "R8" or array.ndim == 2:
            # Mono: the buffer is already single-channel, but its stride can be
            # padded out to a hardware alignment, so trim to the real width.
            frame = array[:out_h, :out_w]
        else:
            # YUV420: the luma plane is the first `height` rows. That is the
            # grayscale image; the chroma planes below it are discarded.
            frame = array[:out_h, :out_w]
        frame = np.ascontiguousarray(frame)

        # SensorTimestamp is nanoseconds on the same clock as CLOCK_MONOTONIC,
        # and is far closer to the actual exposure than anything measured here.
        sensor_ns = self._last_metadata.get("SensorTimestamp")
        t_mono = (sensor_ns / 1e9) if sensor_ns else time.monotonic()
        return frame, FrameMeta(t=time.time(), t_mono=t_mono)

    # ------------------------------------------------------------- read-back
    def hardware_params(self) -> Dict[str, Any]:
        meta = self._last_metadata
        if not meta:
            return {}
        out: Dict[str, Any] = {"mono": self._mono, "format": self._output_format}
        if "ExposureTime" in meta:
            out["exposure_us"] = int(meta["ExposureTime"])
            # Worth surfacing: libcamera does this silently, and a "why is my
            # exposure not what I set" hunt otherwise ends in the datasheet.
            out["exposure_clamped"] = abs(
                int(meta["ExposureTime"]) - self._exposure_us) > 50
        if "AnalogueGain" in meta:
            out["analogue_gain"] = round(float(meta["AnalogueGain"]), 4)
        if "DigitalGain" in meta:
            out["digital_gain"] = round(float(meta["DigitalGain"]), 4)
        if "FrameDuration" in meta:
            duration = int(meta["FrameDuration"])
            out["frame_duration_us"] = duration
            if duration > 0:
                out["fps"] = round(1e6 / duration, 2)
        if "ScalerCrop" in meta:
            out["scaler_crop"] = tuple(int(v) for v in meta["ScalerCrop"])
        return out
