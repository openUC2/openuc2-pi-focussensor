"""HTTP + WebSocket service.

Two channels, deliberately:

* **REST** for anything a human or a controller sets up occasionally —
  exposure, gain, ROI, estimator thresholds, calibration bookkeeping. Plain
  JSON, curl-able, self-documenting at ``/docs``.
* **WebSocket** (``/ws/focus``) for the focus value itself, pushed at the
  acquisition rate. This is the path a lock runs on: no polling, no request
  overhead, and a slow client drops samples instead of slowing the sensor.

Plus a debug video path (``/api/frame.jpg``, ``/api/stream.mjpg``) that exists
purely so aligning the optics and picking an ROI is a visual job rather than a
numeric one. It is rendered on demand, so it costs nothing when unused.

Timestamps on the wire are **seconds** (float): ``t`` is wall-clock
(``time.time()``), ``t_mono`` is monotonic and is the one to use for rate and
staleness maths.
"""

import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from . import overlay
from .config import ParamStore, save_config
from .engine import FocusEngine
from .focus import FocusParams

log = logging.getLogger("focussensor.server")


def create_app(config: Dict[str, Any], engine: Optional[FocusEngine] = None) -> FastAPI:
    """Build the ASGI app. ``engine`` is injectable so tests can supply one."""

    stream_cfg = config.get("stream", {})
    preview_max_width = int(stream_cfg.get("preview_max_width", 800))
    engine = engine or FocusEngine(
        camera_config=config.get("camera", {}),
        focus_params=FocusParams(**config.get("focus", {})),
        history_length=int(stream_cfg.get("history_length", 2000)),
    )

    def snapshot_params() -> Dict[str, Any]:
        """The running settings, in the shape the config file stores them."""
        camera_params = engine.camera.get_params()
        camera = dict(config.get("camera") or {})
        camera["startup"] = {
            "exposure_us": camera_params["exposure_us"],
            "gain": camera_params["gain"],
            "binning": camera_params["binning"],
            "fps_target": camera_params["fps_target"],
            # null means the full sensor; storing it that way keeps the file
            # portable between sensors of different sizes.
            "roi": None if camera_params["is_full_frame"] else camera_params["roi"],
        }
        if getattr(engine.camera, "simulated", False):
            camera["simulation"] = engine.camera.params.to_dict()
        return {"camera": camera, "focus": engine.estimator.params.to_dict()}

    store = ParamStore(config, snapshot_params)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine.start()
        try:
            yield
        finally:
            # Anything still inside the debounce window is written out here,
            # so a clean shutdown never loses the last adjustment.
            store.close()
            engine.close()

    app = FastAPI(
        title="openUC2 Pi focus sensor",
        version="0.1.0",
        description="Two-spot reflection focus sensor: REST for setup, "
                    "WebSocket for the focus value, MJPEG for alignment.",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.config = config

    def _sim_camera():
        camera = engine.camera
        if not getattr(camera, "simulated", False):
            raise HTTPException(status_code=409,
                                detail="the active camera is not a simulation")
        return camera

    # ------------------------------------------------------------------ status
    @app.get("/api/status", tags=["status"])
    def get_status() -> Dict[str, Any]:
        """Everything about the sensor in one call: camera settings, estimator
        parameters, frame rate, error and restart counts."""
        status = engine.get_status()
        status["name"] = config.get("name", "openuc2-focussensor")
        status["api_version"] = 1
        status["simulated"] = bool(getattr(engine.camera, "simulated", False))
        status["server_time"] = time.time()
        status["persistence"] = {
            "enabled": store.enabled,
            "path": config.get("_path"),
            "last_saved": store.last_saved_path,
            "last_error": store.last_error,
        }
        return status

    @app.get("/api/health", tags=["status"])
    def get_health() -> Dict[str, Any]:
        latest = engine.latest
        age = (time.monotonic() - latest["t_mono"]) if latest else None
        return {"ok": engine.running and latest is not None and (age or 0) < 2.0,
                "running": engine.running, "sample_age_s": age}

    # ------------------------------------------------------------------- focus
    @app.get("/api/focus", tags=["focus"])
    def get_focus() -> Dict[str, Any]:
        """The most recent estimate. Poll this if you cannot use the socket."""
        latest = engine.latest
        if latest is None:
            raise HTTPException(status_code=503, detail="no sample yet")
        latest["age_s"] = time.monotonic() - latest["t_mono"]
        return latest

    @app.get("/api/focus/history", tags=["focus"])
    def get_history(limit: int = Query(200, ge=1, le=5000)) -> Dict[str, Any]:
        """The last N samples — enough to plot a calibration sweep."""
        return {"samples": engine.history(limit)}

    @app.delete("/api/focus/history", tags=["focus"])
    def clear_history() -> Dict[str, Any]:
        engine.clear_history()
        return {"ok": True}

    @app.get("/api/focus/projection", tags=["focus"])
    def get_projection() -> Dict[str, Any]:
        """The smoothed x-projection the peaks were found in.

        Useful when a lock misbehaves: it shows whether the estimator saw two
        clean peaks, one peak, or a stray reflection it latched onto.
        """
        projection = engine.latest_projection
        if projection is None:
            raise HTTPException(status_code=503, detail="no sample yet")
        return {"projection": [round(float(v), 3) for v in projection],
                "sample": engine.latest}

    @app.get("/api/focus/params", tags=["focus"])
    def get_focus_params() -> Dict[str, Any]:
        return engine.estimator.params.to_dict()

    @app.post("/api/focus/params", tags=["focus"])
    def set_focus_params(params: Dict[str, Any] = Body(..., examples=[{
        "gaussian_sigma": 3.0, "peak_distance": 40, "peak_prominence_mad": 4.0,
    }])) -> Dict[str, Any]:
        """Update any subset of the estimator parameters.

        Accepts the keys of ``FocusParams``: ``projection_mode``,
        ``background_threshold``, ``baseline_percentile``,
        ``enable_gaussian_blur``, ``gaussian_sigma``, ``peak_distance``,
        ``peak_prominence_mad``, ``peak_height_mad``, ``peak_max_distance``,
        ``left_peak_roi``, ``history_length``, ``outlier_threshold_mad``,
        ``min_quality``.
        """
        try:
            updated = engine.set_focus_params(**params)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.touch()
        return updated

    @app.post("/api/focus/reset", tags=["focus"])
    def reset_focus() -> Dict[str, Any]:
        """Forget the running spot-separation history (after a realignment)."""
        engine.estimator.reset_history()
        return {"ok": True}

    # ------------------------------------------------------------------ camera
    @app.get("/api/camera/params", tags=["camera"])
    def get_camera_params() -> Dict[str, Any]:
        return engine.camera.get_params()

    @app.post("/api/camera/params", tags=["camera"])
    def set_camera_params(params: Dict[str, Any] = Body(..., examples=[{
        "exposure_us": 5000, "gain": 1.0,
        "roi": {"x": 408, "y": 444, "width": 640, "height": 200},
    }])) -> Dict[str, Any]:
        """Set ``exposure_us``, ``gain``, ``binning``, ``fps_target`` and/or
        ``roi`` (``{x, y, width, height}`` in full-sensor pixels).

        ``roi`` takes ``{x, y, width, height}`` in full-sensor pixels, or the
        string ``"full"`` (or null) to open back up to the whole sensor.

        Values are clamped to what the backend accepts rather than rejected,
        so a slider that runs past the hardware limit still works. Changes are
        written back to the config file, so they survive a reboot.
        """
        try:
            updated = engine.set_camera_params(**params)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.touch()
        return updated

    @app.post("/api/camera/roi/full", tags=["camera"])
    def set_roi_full() -> Dict[str, Any]:
        """Open the readout window back up to the whole sensor.

        The shortcut you want when the spots are not where the narrow band is
        pointed and you need to find them again.
        """
        params = engine.set_camera_params(roi="full")
        store.touch()
        return params

    @app.get("/api/camera/limits", tags=["camera"])
    def get_camera_limits() -> Dict[str, Any]:
        return engine.camera.limits.to_dict()

    # ------------------------------------------------------------------ images
    @app.get("/api/frame.jpg", tags=["images"],
             response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
    def get_frame_jpeg(overlay_on: bool = Query(True, alias="overlay"),
                       stretch: bool = Query(True),
                       quality: int = Query(80, ge=1, le=100),
                       scale: float = Query(1.0, gt=0.05, le=4.0),
                       max_width: int = Query(None, ge=32, le=4096)) -> Response:
        """The current ROI as an annotated JPEG — the alignment view.

        Always scaled down to ``stream.preview_max_width`` (pass ``max_width``
        to override). The preview is for looking at; measurements come from
        the focus endpoints, so there is nothing to gain from full-resolution
        JPEGs and a Pi Zero has better things to do with the CPU.
        """
        frame, projection, sample = engine.snapshot()
        if frame is None:
            # Give a starting camera a moment rather than failing a page load.
            engine.wait_for_fresh_sample(timeout=2.0)
            frame, projection, sample = engine.snapshot()
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame yet")
        try:
            jpeg = overlay.render(frame, projection, sample, overlay=overlay_on,
                                  stretch=stretch, quality=quality, scale=scale,
                                  max_width=max_width or preview_max_width)
        except RuntimeError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/frame.npy", tags=["images"],
             response_class=Response,
             responses={200: {"content": {"application/octet-stream": {}}}})
    def get_frame_npy() -> Response:
        """The raw ROI frame as a ``.npy`` buffer.

        ImSwitch's detector reads this for ``getLatestFrame()``, so the focus
        lock widget shows real pixels with no lossy re-encoding in the way.
        """
        frame = engine.latest_frame
        if frame is None:
            engine.wait_for_fresh_sample(timeout=2.0)
            frame = engine.latest_frame
        if frame is None:
            raise HTTPException(status_code=503, detail="no frame yet")
        buffer = io.BytesIO()
        np.save(buffer, frame, allow_pickle=False)
        return Response(content=buffer.getvalue(),
                        media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/stream.mjpg", tags=["images"])
    def get_mjpeg(fps: float = Query(None, gt=0.1, le=30.0),
                  quality: int = Query(None, ge=1, le=100),
                  scale: float = Query(1.0, gt=0.05, le=4.0),
                  max_width: int = Query(None, ge=32, le=4096)) -> StreamingResponse:
        """Motion-JPEG of the annotated ROI, for a browser or ImSwitch.

        Downscaled to ``stream.preview_max_width`` like the single-frame view.
        """
        target_fps = fps or float(stream_cfg.get("mjpeg_fps", 10.0))
        jpeg_quality = quality or int(stream_cfg.get("jpeg_quality", 80))
        boundary = "focusframe"

        async def frames():
            period = 1.0 / target_fps
            while True:
                start = time.monotonic()
                frame, projection, sample = engine.snapshot()
                if frame is not None:
                    try:
                        jpeg = overlay.render(frame, projection, sample,
                                              quality=jpeg_quality, scale=scale,
                                              max_width=max_width or preview_max_width)
                        yield (b"--" + boundary.encode() + b"\r\n"
                               b"Content-Type: image/jpeg\r\n"
                               b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                               + jpeg + b"\r\n")
                    except RuntimeError:
                        return
                await asyncio.sleep(max(0.0, period - (time.monotonic() - start)))

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={"Cache-Control": "no-store"})

    # -------------------------------------------------------------- simulation
    @app.get("/api/sim/state", tags=["simulation"])
    def get_sim_state() -> Dict[str, Any]:
        camera = _sim_camera()
        return {"z_um": camera.z_um, "params": camera.params.to_dict()}

    @app.post("/api/sim/state", tags=["simulation"])
    def set_sim_state(body: Dict[str, Any] = Body(..., examples=[{"z_um": 1.5}])
                      ) -> Dict[str, Any]:
        """Drive the simulated stage, and optionally the optical model.

        ``z_um`` sets an absolute position, ``dz_um`` a relative move, and any
        other key updates ``SimParams``.  With ``wait=true`` (the default) the
        call blocks until a frame exposed *after* the move has been processed
        and returns that sample — so a caller mirroring its own stage position
        in gets the matching focus value back in one round trip, with no
        in-flight frame from the old position.
        """
        camera = _sim_camera()
        wait = bool(body.pop("wait", True))
        z_um, dz_um = body.pop("z_um", None), body.pop("dz_um", None)
        if z_um is not None:
            camera.set_z(float(z_um))
        if dz_um is not None:
            camera.move_z(float(dz_um))
        if body:
            try:
                camera.update_params(**body)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        sample = engine.wait_for_fresh_sample(timeout=1.0) if wait else engine.latest
        return {"ok": True, "z_um": camera.z_um, "sample": sample}

    # ------------------------------------------------------------------ config
    @app.post("/api/params/save", tags=["config"])
    def save_params(path: Optional[str] = Body(None, embed=True)) -> Dict[str, Any]:
        """Write the running settings out now.

        Changes are persisted automatically a few seconds after the last one;
        this forces it immediately, or writes to a different ``path``.
        """
        config.update(snapshot_params())
        try:
            written = save_config(config, path) if path else store.flush()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if written is None:
            raise HTTPException(status_code=500,
                                detail=store.last_error or "write failed")
        return {"ok": True, "path": written}

    # --------------------------------------------------------------- websocket
    @app.websocket("/ws/focus")
    async def ws_focus(websocket: WebSocket) -> None:
        """Push every focus sample as JSON at the acquisition rate.

        ``?decimate=N`` sends every Nth sample, for a UI that does not need the
        full rate. Backlog is dropped rather than queued: a client that stalls
        resumes on the newest data.
        """
        await websocket.accept()
        decimate = 1
        try:
            decimate = max(1, int(websocket.query_params.get("decimate", 1)))
        except (TypeError, ValueError):
            pass

        loop = asyncio.get_running_loop()
        queue = engine.subscribe(loop)
        counter = 0
        try:
            while True:
                payload = await queue.get()
                counter += 1
                if counter % decimate:
                    continue
                await websocket.send_json(payload)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            engine.unsubscribe(queue)

    # -------------------------------------------------------------- debug page
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return _DEBUG_PAGE

    return app


_DEBUG_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>openUC2 focus sensor</title>
<style>
 :root { color-scheme: dark; }
 body { background:#0d0f12; color:#dfe4ea; font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;
        margin:0; padding:20px; }
 h1 { font-size:16px; letter-spacing:.08em; text-transform:uppercase; color:#7f8c9b;
      margin:0 0 16px; font-weight:600; }
 img { max-width:100%; border:1px solid #222831; border-radius:4px; display:block; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px;
         margin:16px 0; }
 .cell { background:#151920; border:1px solid #222831; border-radius:4px; padding:10px 12px; }
 .cell .k { color:#7f8c9b; font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
 .cell .v { font-size:20px; margin-top:2px; }
 .bad { color:#ff6b6b; } .good { color:#63e6be; }
 label { color:#7f8c9b; margin-right:4px; }
 input { background:#0d0f12; color:#dfe4ea; border:1px solid #333c47; border-radius:3px;
         padding:4px 6px; width:80px; font:inherit; }
 button { background:#1f6feb; color:#fff; border:0; border-radius:3px; padding:5px 12px;
          font:inherit; cursor:pointer; }
 .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
 a { color:#58a6ff; }
</style></head><body>
<h1>openUC2 focus sensor</h1>
<img src="/api/stream.mjpg" alt="focus sensor view">
<div class="grid">
  <div class="cell"><div class="k">focus (px)</div><div class="v" id="focus">–</div></div>
  <div class="cell"><div class="k">separation (px)</div><div class="v" id="sep">–</div></div>
  <div class="cell"><div class="k">quality (MAD)</div><div class="v" id="q">–</div></div>
  <div class="cell"><div class="k">rate (Hz)</div><div class="v" id="rate">–</div></div>
  <div class="cell"><div class="k">sim z (um)</div><div class="v" id="z">–</div></div>
</div>
<div class="row">
  <label>exposure us</label><input id="exp" type="number" step="100">
  <label>gain</label><input id="gain" type="number" step="0.1">
  <label>sigma</label><input id="sigma" type="number" step="0.5">
  <button onclick="apply()">apply</button>
  <button onclick="save()">save to config</button>
</div>
<div class="row" id="simrow" hidden>
  <label>set sim z (um)</label><input id="simz" type="number" step="0.5">
  <button onclick="setZ()">move</button>
</div>
<p><a href="/docs">REST API docs</a> · <a href="/api/frame.jpg">single frame</a></p>
<script>
let n = 0, t0 = performance.now();
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/focus');
ws.onmessage = (e) => {
  const s = JSON.parse(e.data); n++;
  const f = document.getElementById('focus');
  f.textContent = s.focus === null ? '–' : s.focus.toFixed(2);
  f.className = 'v ' + (s.valid ? 'good' : 'bad');
  document.getElementById('sep').textContent =
    s.x_peak_distance === null ? '–' : s.x_peak_distance.toFixed(2);
  document.getElementById('q').textContent = s.quality.toFixed(1);
  document.getElementById('z').textContent = s.z_um === null ? '–' : s.z_um.toFixed(3);
  const dt = (performance.now() - t0) / 1000;
  if (dt > 0.5) { document.getElementById('rate').textContent = (n / dt).toFixed(0); n = 0; t0 = performance.now(); }
};
async function refresh() {
  const st = await (await fetch('/api/status')).json();
  document.getElementById('exp').value = st.camera.exposure_us;
  document.getElementById('gain').value = st.camera.gain;
  document.getElementById('sigma').value = st.focus_params.gaussian_sigma;
  document.getElementById('simrow').hidden = !st.simulated;
}
const post = (url, body) => fetch(url, {method: 'POST',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
async function apply() {
  await post('/api/camera/params', {exposure_us: +document.getElementById('exp').value,
                                    gain: +document.getElementById('gain').value});
  await post('/api/focus/params', {gaussian_sigma: +document.getElementById('sigma').value});
}
const save = () => post('/api/params/save', {});
const setZ = () => post('/api/sim/state', {z_um: +document.getElementById('simz').value});
refresh();
</script></body></html>
"""
