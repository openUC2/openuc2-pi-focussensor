"""Parameters survive a restart, and previews are always downscaled."""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "software"))

from fastapi.testclient import TestClient          # noqa: E402

from focussensor.config import DEFAULTS, load_config, save_config   # noqa: E402
from focussensor.server import create_app                           # noqa: E402


@pytest.fixture
def config_path(tmp_path):
    """A config file on disk, seeded with the shipped defaults."""
    path = tmp_path / "focussensor.yaml"
    config = {k: v for k, v in DEFAULTS.items() if not k.startswith("_")}
    config["camera"] = dict(config["camera"], backend="simulated")
    config["persist_delay_s"] = 0.2       # keep the test quick
    save_config(config, str(path))
    return path


def _client(config_path):
    config = load_config(str(config_path))
    return TestClient(create_app(config))


def test_fresh_install_starts_on_the_full_sensor(config_path):
    with _client(config_path) as client:
        camera = client.get("/api/camera/params").json()
        assert camera["is_full_frame"] is True
        assert camera["roi"]["width"] == camera["full_shape"]["width"]


def test_parameters_survive_a_restart(config_path):
    with _client(config_path) as client:
        client.post("/api/camera/params", json={
            "exposure_us": 12345, "gain": 3.5, "fps_target": 40.0,
            "roi": {"x": 300, "y": 400, "width": 800, "height": 240}})
        client.post("/api/focus/params", json={
            "gaussian_sigma": 4.5, "peak_distance": 55, "min_quality": 20.0})
        # Forced rather than waiting out the debounce, but the shutdown hook
        # below covers the un-forced path too.
        assert client.post("/api/params/save", json={}).json()["ok"]

    # A second process, reading only what was written to disk.
    with _client(config_path) as client:
        camera = client.get("/api/camera/params").json()
        assert camera["exposure_us"] == 12345
        assert camera["gain"] == 3.5
        assert camera["fps_target"] == 40.0
        assert camera["roi"] == {"x": 300, "y": 400, "width": 800, "height": 240}
        assert camera["is_full_frame"] is False

        focus = client.get("/api/focus/params").json()
        assert focus["gaussian_sigma"] == 4.5
        assert focus["peak_distance"] == 55
        assert focus["min_quality"] == 20.0


def test_shutdown_flushes_a_pending_change(config_path):
    """A change inside the debounce window is not lost when the service stops."""
    with _client(config_path) as client:
        client.post("/api/camera/params", json={"exposure_us": 7777})
        # No save call and no wait: only the shutdown hook can persist this.

    with _client(config_path) as client:
        assert client.get("/api/camera/params").json()["exposure_us"] == 7777


def test_full_frame_is_stored_as_null_not_as_pixels(config_path):
    """So a saved config stays portable between differently sized sensors."""
    import yaml

    with _client(config_path) as client:
        client.post("/api/camera/params",
                    json={"roi": {"x": 300, "y": 400, "width": 800, "height": 240}})
        client.post("/api/camera/roi/full")
        client.post("/api/params/save", json={})

    stored = yaml.safe_load(config_path.read_text())
    assert stored["camera"]["startup"]["roi"] is None


def test_roi_can_always_be_reopened_to_full(config_path):
    with _client(config_path) as client:
        client.post("/api/camera/params",
                    json={"roi": {"x": 300, "y": 400, "width": 640, "height": 200}})
        assert client.get("/api/camera/params").json()["is_full_frame"] is False
        assert client.post("/api/camera/roi/full").json()["is_full_frame"] is True
        # The string form of the same thing.
        client.post("/api/camera/params",
                    json={"roi": {"x": 0, "y": 0, "width": 640, "height": 200}})
        assert client.post("/api/camera/params",
                           json={"roi": "full"}).json()["is_full_frame"] is True


def test_persistence_can_be_switched_off(config_path):
    import yaml

    config = yaml.safe_load(config_path.read_text())
    config["persist_params"] = False
    save_config(config, str(config_path))

    with _client(config_path) as client:
        assert client.get("/api/status").json()["persistence"]["enabled"] is False
        client.post("/api/camera/params", json={"exposure_us": 4242})

    with _client(config_path) as client:
        assert client.get("/api/camera/params").json()["exposure_us"] != 4242


def test_preview_is_downscaled_by_default(config_path):
    from PIL import Image

    with _client(config_path) as client:
        # Full sensor, so an unscaled preview would be 1456 px wide.
        assert client.get("/api/camera/params").json()["is_full_frame"] is True
        response = client.get("/api/frame.jpg")
        assert response.status_code == 200
        image = Image.open(io.BytesIO(response.content))
        assert image.width == DEFAULTS["stream"]["preview_max_width"]

        smaller = client.get("/api/frame.jpg", params={"max_width": 320})
        assert Image.open(io.BytesIO(smaller.content)).width == 320
        assert len(smaller.content) < len(response.content)


def test_measurement_frames_are_never_downscaled(config_path):
    """The preview shrinks; the data the estimator and ImSwitch see does not."""
    import numpy as np

    with _client(config_path) as client:
        full = client.get("/api/camera/params").json()["full_shape"]
        raw = np.load(io.BytesIO(client.get("/api/frame.npy").content))
        assert raw.shape == (full["height"], full["width"])
