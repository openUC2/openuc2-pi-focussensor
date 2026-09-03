"""Raspberry Pi camera backend (libcamera / picamera2).

Written against the same ``CameraBase`` contract as the simulator, so nothing
above this file changes when you swap the simulation for real optics.

**Not yet verified on hardware** — the simulator is the tested path today.

Design notes that matter for a focus sensor, as opposed to a picture camera:

* Every automatic algorithm is switched **off** (AE, AWB, denoise). A focus
  sensor is a measuring instrument; an auto-exposure loop hunting between
  frames would show up directly as focus noise.
* The ROI is applied as ``ScalerCrop`` in *sensor* coordinates with the output
  size set to the same dimensions. The ISP then delivers those pixels 1:1 with
  no scaling, at a frame rate set by the window size rather than the full
  sensor — this is what makes a 640x200 window run at hundreds of fps instead
  of grabbing full frames and cropping in numpy.
* Frames are requested as YUV420 and only the luma plane is used, which is the
  cheapest thing the ISP can hand over. On a mono sensor (IMX296 mono) the
  luma plane is the sensor data.
* ``FrameDurationLimits`` pins the frame period; exposure is clamped below it,
  because libcamera silently lowers the frame rate to fit a long exposure.
"""

import time
from typing import Optional, Tuple

import numpy as np

from .base import CameraBase, CameraLimits, FrameMeta


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
        limits = limits or CameraLimits(full_width=int(full_w), full_height=int(full_h))
        limits.full_width, limits.full_height = int(full_w), int(full_h)

        ctrl_limits = self._picam2.camera_controls
        if "ExposureTime" in ctrl_limits:
            limits.exposure_us_min = int(ctrl_limits["ExposureTime"][0])
            limits.exposure_us_max = int(ctrl_limits["ExposureTime"][1])
        if "AnalogueGain" in ctrl_limits:
            limits.gain_min = float(ctrl_limits["AnalogueGain"][0])
            limits.gain_max = float(ctrl_limits["AnalogueGain"][1])

        self.model = f"picamera2:{props.get('Model', 'unknown')}"
        super().__init__(limits)
        self._configured_roi: Optional[Tuple[int, int, int, int]] = None
        self.center_roi(640, 200)

    # ------------------------------------------------------------------ config
    def _controls(self) -> dict:
        frame_us = int(1e6 / max(0.1, self._fps_target))
        # Exposure must fit inside the frame period or libcamera stretches it.
        exposure = min(self._exposure_us, max(self._limits.exposure_us_min, frame_us - 50))
        return {
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": int(exposure),
            "AnalogueGain": float(self._gain),
            "FrameDurationLimits": (frame_us, frame_us),
            "NoiseReductionMode": 0,
        }

    def _configure(self) -> None:
        """(Re)build the libcamera configuration for the current ROI/binning."""
        x, y, w, h = self._roi
        out_w, out_h = w // self._binning, h // self._binning
        # libcamera wants even dimensions for YUV420.
        out_w -= out_w % 2
        out_h -= out_h % 2

        was_running = self._picam2.started
        if was_running:
            self._picam2.stop()

        config = self._picam2.create_video_configuration(
            main={"size": (out_w, out_h), "format": "YUV420"},
            controls=self._controls(),
            buffer_count=4,
            queue=False,   # always hand back the newest frame, never a stale one
        )
        self._picam2.configure(config)
        self._picam2.set_controls({"ScalerCrop": (x, y, w, h)})
        self._configured_roi = self._roi
        if was_running:
            self._picam2.start()

    # --------------------------------------------------------------- lifecycle
    def _on_start(self) -> None:
        if self._configured_roi != self._roi:
            self._configure()
        self._picam2.start()
        self._picam2.set_controls(self._controls())
        time.sleep(0.2)   # let the first frames flush through with the new controls

    def _on_stop(self) -> None:
        if self._picam2.started:
            self._picam2.stop()

    def close(self) -> None:
        super().close()
        try:
            self._picam2.close()
        except Exception:
            pass

    # ----------------------------------------------------------------- setters
    def _apply_exposure(self, exposure_us: int) -> None:
        if self._picam2.started:
            self._picam2.set_controls(self._controls())

    def _apply_gain(self, gain: float) -> None:
        if self._picam2.started:
            self._picam2.set_controls(self._controls())

    def _apply_fps(self, fps: float) -> None:
        if self._picam2.started:
            self._picam2.set_controls(self._controls())

    def _apply_roi(self, roi: Tuple[int, int, int, int]) -> None:
        # A window change is a reconfiguration, not a control write.
        if self._configured_roi is not None:
            self._configure()

    def _apply_binning(self, binning: int) -> None:
        if self._configured_roi is not None:
            self._configure()

    # ------------------------------------------------------------------- grab
    def _grab(self) -> Tuple[np.ndarray, FrameMeta]:
        request = self._picam2.capture_request()
        try:
            array = request.make_array("main")
            metadata = request.get_metadata()
        finally:
            request.release()

        # YUV420: the luma plane is the first `height` rows.
        out_h = self.shape[1] - self.shape[1] % 2
        frame = np.ascontiguousarray(array[:out_h, : self.shape[0]])

        # SensorTimestamp is nanoseconds on the same clock as CLOCK_MONOTONIC.
        sensor_ns = metadata.get("SensorTimestamp")
        t_mono = (sensor_ns / 1e9) if sensor_ns else time.monotonic()
        return frame, FrameMeta(t=time.time(), t_mono=t_mono)
