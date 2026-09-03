"""Tests for the Pi camera backend, against a stand-in picamera2.

The sensor cannot be exercised in CI, but almost everything that goes wrong
with this backend is not the sensor: it is the control set, the crop
arithmetic, the format fallback and the read-back. A fake picamera2 that
behaves like the real one -- advertising a limited control set, rejecting an
unsupported format, clamping exposure -- catches all of that.
"""

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "software"))


class FakeRequest:
    def __init__(self, array, metadata):
        self._array, self._metadata = array, metadata
        self.released = False

    def make_array(self, _name):
        return self._array

    def get_metadata(self):
        return self._metadata

    def release(self):
        self.released = True


class FakePicamera2:
    """Mimics the parts of picamera2 this backend touches."""

    def __init__(self, camera_num=0, tuning=None, *, mono=False, allow_r8=True):
        self.camera_num = camera_num
        self.started = False
        self.mono, self.allow_r8 = mono, allow_r8
        self.config = None
        self.applied = {}
        self.closed = False
        self.configure_calls = 0

        self.camera_properties = {
            "PixelArraySize": (1456, 1088),
            "ScalerCropMaximum": (8, 4, 1440, 1080),
            "Model": "imx296" if mono else "imx708",
        }
        fmt = "R10_CSI2P" if mono else "SRGGB10_CSI2P"
        self.sensor_modes = [{"format": fmt, "size": (1456, 1088)}]
        # Deliberately omits ColourGains and AeFlickerMode: a real pipeline
        # does not advertise every control, and setting a missing one raises.
        self.camera_controls = {
            "ExposureTime": (60, 120_000, 10_000),
            "AnalogueGain": (1.0, 16.0, 1.0),
            "FrameDurationLimits": (5_000, 1_000_000, 33_333),
            "NoiseReductionMode": (0, 4, 0),
            "Sharpness": (0.0, 16.0, 1.0),
            "Contrast": (0.0, 32.0, 1.0),
            "Brightness": (-1.0, 1.0, 0.0),
            "Saturation": (0.0, 32.0, 1.0),
            "AeEnable": (False, True, True),
            "AwbEnable": (False, True, True),
            "ScalerCrop": ((0, 0, 0, 0), (65535,) * 4, (0, 0, 1456, 1088)),
        }

    def create_video_configuration(self, main=None, controls=None, **kwargs):
        if main["format"] == "R8" and not self.allow_r8:
            raise RuntimeError("R8 not supported by this pipeline")
        return {"main": dict(main), "controls": dict(controls or {}), **kwargs}

    def configure(self, config):
        self.config = config
        self.configure_calls += 1
        self.applied.update(config.get("controls", {}))

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def set_controls(self, controls):
        unknown = set(controls) - set(self.camera_controls) - {"ScalerCrop"}
        if unknown:
            raise RuntimeError(f"unsupported controls: {sorted(unknown)}")
        self.applied.update(controls)

    def capture_request(self):
        w, h = self.config["main"]["size"]
        fmt = self.config["main"]["format"]
        # YUV420 buffers arrive with the chroma planes appended below luma, and
        # a stride that can be padded wider than the requested width.
        array = (np.full((h, w + 8), 40, np.uint8) if fmt == "R8"
                 else np.full((h * 3 // 2, w + 8), 40, np.uint8))
        requested = self.applied.get("ExposureTime", 5000)
        duration = self.applied.get("FrameDurationLimits", (33_333, 33_333))[0]
        metadata = {
            "ExposureTime": min(requested, duration - 100),   # the real clamp
            "AnalogueGain": self.applied.get("AnalogueGain", 1.0),
            "DigitalGain": 1.0,
            "FrameDuration": duration,
            "SensorTimestamp": 1_234_567_890_000,
            "ScalerCrop": self.applied.get("ScalerCrop", (0, 0, 1456, 1088)),
        }
        return FakeRequest(array, metadata)


@pytest.fixture
def picamera2_module(monkeypatch):
    """Install a fake ``picamera2`` package for the duration of a test."""
    made = {}

    def install(**kwargs):
        module = types.ModuleType("picamera2")

        def factory(camera_num=0, tuning=None):
            cam = FakePicamera2(camera_num, tuning, **kwargs)
            made["camera"] = cam
            return cam

        module.Picamera2 = factory
        module.Picamera2.load_tuning_file = staticmethod(lambda name: {"name": name})
        monkeypatch.setitem(sys.modules, "picamera2", module)
        for name in list(sys.modules):
            if name.endswith("picamera2_backend"):
                del sys.modules[name]
        from focussensor.cameras.picamera2_backend import Picamera2Camera
        return Picamera2Camera(), made

    return install


def test_starts_on_the_full_sensor(picamera2_module):
    camera, _ = picamera2_module()
    assert camera.roi == (0, 0, 1456, 1088)
    assert camera.get_params()["is_full_frame"] is True
    assert camera.full_shape == (1456, 1088)


def test_limits_come_from_the_pipeline(picamera2_module):
    camera, _ = picamera2_module()
    assert camera.limits.exposure_us_min == 60
    assert camera.limits.exposure_us_max == 120_000
    assert camera.limits.gain_max == 16.0
    assert camera.limits.fps_max == pytest.approx(200.0, abs=0.1)


def test_mono_sensor_is_detected_and_read_as_r8(picamera2_module):
    camera, made = picamera2_module(mono=True)
    assert "mono" in camera.model
    camera.set_roi(100, 100, 640, 200)
    camera.start()
    frame, _ = camera.grab()
    assert made["camera"].config["main"]["format"] == "R8"
    assert frame.shape == (200, 640)          # stride padding trimmed off
    camera.close()


def test_colour_sensor_uses_the_luma_plane(picamera2_module):
    camera, made = picamera2_module(mono=False)
    assert "colour" in camera.model
    camera.set_roi(0, 0, 640, 200)
    camera.start()
    frame, _ = camera.grab()
    assert made["camera"].config["main"]["format"] == "YUV420"
    # Only the luma rows, not the chroma planes underneath them.
    assert frame.shape == (200, 640)
    camera.close()


def test_falls_back_to_yuv_when_r8_is_rejected(picamera2_module):
    camera, made = picamera2_module(mono=True, allow_r8=False)
    camera.start()
    camera.grab()
    assert made["camera"].config["main"]["format"] == "YUV420"
    camera.close()


def test_every_automatic_algorithm_is_disabled(picamera2_module):
    camera, made = picamera2_module()
    camera.start()
    applied = made["camera"].applied
    assert applied["AeEnable"] is False
    assert applied["AwbEnable"] is False
    assert applied["NoiseReductionMode"] == 0
    assert applied["Sharpness"] == 0.0
    assert applied["Saturation"] == 0.0
    assert applied["Contrast"] == 1.0
    camera.close()


def test_controls_the_pipeline_does_not_advertise_are_not_sent(picamera2_module):
    # The fake raises on an unknown control, exactly like libcamera. It does
    # not advertise ColourGains (mono pipelines often do not), so a backend
    # that sent it unconditionally would fail here.
    camera, made = picamera2_module()
    camera.start()
    camera.exposure_us = 9000
    camera.gain = 4.0
    assert "ColourGains" not in made["camera"].applied
    assert "AeFlickerMode" not in made["camera"].applied
    camera.close()


def test_exposure_is_clamped_below_the_frame_period(picamera2_module):
    camera, made = picamera2_module()
    camera.fps_target = 100.0          # 10 000 us period
    camera.exposure_us = 50_000        # longer than the period
    camera.start()
    assert made["camera"].applied["ExposureTime"] <= 10_000
    camera.close()


def test_hardware_params_report_the_achieved_settings(picamera2_module):
    camera, _ = picamera2_module()
    camera.fps_target = 50.0
    camera.exposure_us = 5000
    camera.gain = 3.0
    camera.start()
    camera.grab()
    actual = camera.hardware_params()
    assert actual["exposure_us"] == 5000
    assert actual["analogue_gain"] == 3.0
    assert actual["fps"] == pytest.approx(50.0, abs=0.1)
    assert actual["exposure_clamped"] is False

    # Now ask for more exposure than the frame period allows.
    camera.fps_target = 500.0          # 2000 us period
    camera.exposure_us = 20_000
    camera.grab()
    actual = camera.hardware_params()
    assert actual["exposure_us"] < 20_000
    assert actual["exposure_clamped"] is True
    camera.close()


def test_scaler_crop_is_clamped_into_the_allowed_rectangle(picamera2_module):
    camera, made = picamera2_module()
    camera.start()
    camera.set_roi(0, 0, 1456, 1088)
    x, y, w, h = made["camera"].applied["ScalerCrop"]
    cx, cy, cw, ch = made["camera"].camera_properties["ScalerCropMaximum"]
    assert cx <= x and cy <= y
    assert x + w <= cx + cw and y + h <= cy + ch
    camera.close()


def test_roi_change_reconfigures_but_exposure_does_not(picamera2_module):
    camera, made = picamera2_module()
    camera.start()
    before = made["camera"].configure_calls
    camera.exposure_us = 7000
    camera.gain = 2.0
    camera.fps_target = 30.0
    assert made["camera"].configure_calls == before, "control write reconfigured"
    camera.set_roi(200, 200, 640, 200)
    assert made["camera"].configure_calls > before, "ROI change did not reconfigure"
    camera.close()


def test_returning_to_full_frame_works_after_narrowing(picamera2_module):
    camera, _ = picamera2_module()
    camera.start()
    camera.set_roi(400, 400, 640, 200)
    assert camera.get_params()["is_full_frame"] is False
    camera.set_params(roi="full")
    assert camera.roi == (0, 0, 1456, 1088)
    assert camera.get_params()["is_full_frame"] is True
    frame, _ = camera.grab()
    assert frame.shape == (1088, 1456)
    camera.close()
