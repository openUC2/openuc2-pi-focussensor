"""Unit tests for the focus sensor.

Everything here runs against the simulated camera, so the whole suite is
hardware-free and runs in CI.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "software"))

from focussensor import dsp                                          # noqa: E402
from focussensor.cameras import create_camera                        # noqa: E402
from focussensor.cameras.simulated import SimParams, SimulatedTwoSpotCamera  # noqa: E402
from focussensor.engine import FocusEngine                           # noqa: E402
from focussensor.focus import FocusParams, PeakFocusEstimator        # noqa: E402


# ------------------------------------------------------------------------ dsp
def _two_peak_signal(centres=(120, 260), amps=(50, 30), n=400, sigma=6.0, seed=0):
    x = np.zeros(n, dtype=np.float32)
    grid = np.arange(n)
    for c, a in zip(centres, amps):
        x += a * np.exp(-0.5 * ((grid - c) / sigma) ** 2)
    return x + np.random.default_rng(seed).normal(0, 0.5, n).astype(np.float32)


def test_gaussian_filter_matches_scipy_when_available():
    x = _two_peak_signal()
    mine = dsp.gaussian_filter1d(x, 3.0)
    scipy_ndimage = pytest.importorskip("scipy.ndimage")
    theirs = scipy_ndimage.gaussian_filter1d(x, 3.0)
    assert np.allclose(mine, theirs, atol=1e-4)


def test_find_peaks_matches_scipy_when_available():
    smoothed = dsp.gaussian_filter1d(_two_peak_signal(), 3.0)
    mine, props = dsp.find_peaks(smoothed, height=5, prominence=5, distance=50)
    scipy_signal = pytest.importorskip("scipy.signal")
    theirs, _ = scipy_signal.find_peaks(smoothed, height=5, prominence=5, distance=50)
    assert list(mine) == list(theirs)
    assert props["prominences"].shape == mine.shape


def test_find_peaks_distance_keeps_the_taller_peak():
    smoothed = dsp.gaussian_filter1d(
        _two_peak_signal(centres=(120, 150), amps=(50, 30)), 3.0)
    peaks, _ = dsp.find_peaks(smoothed, height=5, prominence=5, distance=60)
    assert len(peaks) == 1 and abs(peaks[0] - 120) <= 2


def test_parabolic_subpixel_is_accurate():
    grid = np.arange(41, dtype=np.float32)
    signal = np.exp(-0.5 * ((grid - 20.35) / 3.0) ** 2)
    assert abs(dsp.parabolic_subpixel(signal, 20) - 20.35) < 0.05


# -------------------------------------------------------------------- metrics
def _synthetic_frame(x_left, x_right, height=120, width=640, seed=1):
    yy, xx = np.mgrid[0:height, 0:width]
    frame = np.full((height, width), 8.0, dtype=np.float32)
    for x, amp in ((x_left, 180.0), (x_right, 110.0)):
        frame += amp * np.exp(-0.5 * (((xx - x) / 5.0) ** 2
                                      + ((yy - height / 2) / 6.0) ** 2))
    frame += np.random.default_rng(seed).normal(0, 2, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def test_estimator_finds_both_spots_with_subpixel_accuracy():
    estimator = PeakFocusEstimator(FocusParams(gaussian_sigma=3.0, peak_distance=40))
    sample, projection = estimator.compute(_synthetic_frame(200, 420),
                                           saturation_value=255)
    # n_peaks counts candidates that passed the thresholds; noise routinely
    # contributes a weak third. What matters is which two were selected.
    assert sample.valid and sample.n_peaks >= 2
    assert abs(sample.focus - 200) < 0.5
    assert abs(sample.right_peak_x - 420) < 0.5
    assert abs(sample.x_peak_distance - 220) < 1.0
    assert len(projection) == 640


def test_min_quality_separates_noise_from_signal():
    """Pure noise must not be handed to the PI loop as a position.

    The MAD scaling means noise still produces "peaks"; ``min_quality`` is what
    rejects them. Measured: noise peaks below ~6 MAD, a real frame reaches
    several hundred, so the shipped default of 10 sits cleanly between.
    """
    params = FocusParams(gaussian_sigma=3.0, peak_distance=40, min_quality=10.0)

    worst_noise = 0.0
    for seed in range(20):
        noise = np.random.default_rng(seed).normal(20, 2, (120, 640))
        noise = noise.clip(0, 255).astype(np.uint8)
        sample, _ = PeakFocusEstimator(params).compute(noise, saturation_value=255)
        worst_noise = max(worst_noise, sample.quality)
        assert not sample.valid

    signal, _ = PeakFocusEstimator(params).compute(_synthetic_frame(200, 420),
                                                   saturation_value=255)
    assert signal.valid
    assert signal.quality > 10 * worst_noise


def test_estimator_rejects_outlier_separations():
    estimator = PeakFocusEstimator(FocusParams(gaussian_sigma=3.0, peak_distance=40))
    for _ in range(5):
        estimator.compute(_synthetic_frame(200, 420))
    steady = estimator.average_separation
    estimator.compute(_synthetic_frame(200, 600))   # a wild jump
    assert estimator.average_separation == pytest.approx(steady, abs=1.0)


# --------------------------------------------------------------------- camera
def test_simulated_camera_spot_moves_linearly_with_z():
    camera = SimulatedTwoSpotCamera(
        params=SimParams(drift_um_per_s=0.0, jitter_um_rms=0.0,
                         sensitivity_px_per_um=3.0, capture_range_um=1e6))
    camera.fps_target = 1000
    camera.start()
    estimator = PeakFocusEstimator(FocusParams(gaussian_sigma=3.0, peak_distance=40))

    positions, values = [], []
    for z in (-4.0, -2.0, 0.0, 2.0, 4.0):
        camera.set_z(z)
        camera.grab()                       # discard the in-flight frame
        frame, _ = camera.grab()
        sample, _ = estimator.compute(frame, saturation_value=camera.saturation_value)
        assert sample.valid
        positions.append(z)
        values.append(sample.focus)

    slope = np.polyfit(positions, values, 1)[0]
    assert slope == pytest.approx(3.0, abs=0.05)
    camera.close()


def test_camera_roi_and_binning_shape_the_frame():
    camera = create_camera({"backend": "simulated"})
    camera.set_roi(100, 200, 320, 160)
    camera.start()
    frame, meta = camera.grab()
    assert frame.shape == (160, 320)
    assert meta.roi == (100, 200, 320, 160)

    camera.binning = 2
    camera.set_roi(100, 200, 320, 160)
    frame, _ = camera.grab()
    assert frame.shape == (80, 160)
    camera.close()


def test_camera_clamps_out_of_range_settings():
    camera = create_camera({"backend": "simulated"})
    camera.exposure_us = 10 ** 9
    assert camera.exposure_us == camera.limits.exposure_us_max
    camera.gain = -5
    assert camera.gain == camera.limits.gain_min
    with pytest.raises(ValueError):
        camera.binning = 3
    camera.close()


def test_exposure_and_gain_change_brightness():
    camera = SimulatedTwoSpotCamera(
        params=SimParams(drift_um_per_s=0.0, jitter_um_rms=0.0, shot_noise=False))
    camera.fps_target = 1000
    camera.start()
    camera.exposure_us = 2000
    dim = camera.grab()[0].astype(float).max()
    camera.exposure_us = 8000
    bright = camera.grab()[0].astype(float).max()
    assert bright > dim
    camera.close()


# --------------------------------------------------------------------- engine
def test_engine_produces_valid_samples_and_stats():
    engine = FocusEngine({"backend": "simulated", "startup": {"fps_target": 200}},
                         FocusParams(gaussian_sigma=3.0, peak_distance=40))
    engine.start()
    try:
        sample = engine.wait_for_fresh_sample(timeout=5.0)
        assert sample is not None and sample["valid"]
        assert sample["focus"] is not None
        frame, projection, snap = engine.snapshot()
        assert frame is not None and projection is not None and snap is not None
        assert engine.get_status()["stats"]["errors"] == 0
        assert len(engine.history(10)) >= 1
    finally:
        engine.close()


def test_engine_tracks_simulated_stage_moves():
    engine = FocusEngine({"backend": "simulated", "startup": {"fps_target": 200}},
                         FocusParams(gaussian_sigma=3.0, peak_distance=40))
    engine.start()
    try:
        engine.camera.set_z(-5.0)
        low = engine.wait_for_fresh_sample(timeout=5.0)["focus"]
        engine.camera.set_z(5.0)
        high = engine.wait_for_fresh_sample(timeout=5.0)["focus"]
        assert high > low + 10          # ~30 px at 3 px/um over 10 um
    finally:
        engine.close()
