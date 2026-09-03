"""Acquisition + estimation loop.

One background thread does: grab a frame, estimate the focus, publish. Nothing
in the HTTP layer ever touches the camera directly, so a slow or disconnected
client can never stall acquisition.

Publishing is fan-out to bounded queues with **drop-oldest** semantics: a
WebSocket client that cannot keep up loses intermediate samples rather than
applying back-pressure to the sensor. For a focus lock that is the right
trade — a sample from 20 ms ago is worthless, the next one is already here.

The loop also owns a small watchdog: if the camera raises repeatedly it is
stopped, reopened and restarted, and the failure is counted in ``stats`` so
``/api/status`` can show it instead of the service silently going quiet.
"""

import asyncio
import copy
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from .cameras import CameraBase, create_camera
from .focus import FocusParams, FocusSample, PeakFocusEstimator

log = logging.getLogger("focussensor.engine")


class FocusEngine:
    """Runs the camera, estimates focus, and fans samples out to subscribers."""

    def __init__(self, camera_config: Optional[Dict[str, Any]] = None,
                 focus_params: Optional[FocusParams] = None,
                 history_length: int = 2000):
        self._camera_config = dict(camera_config or {})
        self.camera: CameraBase = create_camera(self._camera_config)
        self.estimator = PeakFocusEstimator(focus_params)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._latest: Optional[FocusSample] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_projection: Optional[np.ndarray] = None
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_length)

        self._subscribers: List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sub_lock = threading.Lock()

        self.stats: Dict[str, Any] = {
            "frames": 0, "valid": 0, "errors": 0, "restarts": 0,
            "fps": 0.0, "compute_ms": 0.0, "started_at": None, "last_error": None,
        }

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.camera.start()
            self.stats["started_at"] = time.time()
            self._thread = threading.Thread(target=self._run, name="focus-engine",
                                            daemon=True)
            self._thread.start()
            log.info("engine started on %s", self.camera.model)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        try:
            self.camera.stop()
        except Exception:
            log.warning("camera stop failed", exc_info=True)

    def close(self) -> None:
        self.stop()
        try:
            self.camera.close()
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        consecutive_errors = 0
        fps_ema: Optional[float] = None
        last_t: Optional[float] = None

        while not self._stop.is_set():
            try:
                frame, meta = self.camera.grab()
                sample, projection = self.estimator.compute(
                    frame, saturation_value=self.camera.saturation_value)

                # The camera's timestamps win: they are closer to the actual
                # exposure than anything measured after the estimator ran.
                sample.t = meta.t
                sample.t_mono = meta.t_mono
                sample.seq = meta.seq
                sample.z_um = meta.z_um

                with self._lock:
                    self._latest = sample
                    self._latest_frame = frame
                    self._latest_projection = projection
                    self._history.append(sample.to_dict())
                    self.stats["frames"] += 1
                    if sample.valid:
                        self.stats["valid"] += 1
                    if last_t is not None:
                        dt = meta.t_mono - last_t
                        if dt > 0:
                            inst = 1.0 / dt
                            fps_ema = inst if fps_ema is None else 0.9 * fps_ema + 0.1 * inst
                            self.stats["fps"] = round(fps_ema, 2)
                    last_t = meta.t_mono
                    self.stats["compute_ms"] = round(
                        0.9 * self.stats["compute_ms"] + 0.1 * sample.compute_ms, 3)

                self._publish(sample)
                consecutive_errors = 0

            except Exception as exc:  # noqa: BLE001 - the loop must survive
                consecutive_errors += 1
                with self._lock:
                    self.stats["errors"] += 1
                    self.stats["last_error"] = repr(exc)
                log.warning("acquisition error (%d in a row): %s", consecutive_errors, exc)
                if consecutive_errors >= 5:
                    self._restart_camera()
                    consecutive_errors = 0
                else:
                    self._stop.wait(0.1)

    def _restart_camera(self) -> None:
        """Reopen the camera in place after repeated failures."""
        log.error("restarting camera after repeated failures")
        # Snapshot the settings first: they must survive the teardown, so that
        # a restart comes back on the same ROI and exposure rather than
        # silently reverting to defaults mid-experiment.
        try:
            roi = self.camera.roi
            params = self.camera.get_params()
        except Exception:
            roi, params = None, {}
        try:
            self.camera.close()
        except Exception:
            pass
        try:
            self.camera = create_camera(self._camera_config)
            if params:
                self.camera.set_params(
                    exposure_us=params["exposure_us"], gain=params["gain"],
                    binning=params["binning"], fps_target=params["fps_target"],
                    roi=roi)
            self.camera.start()
            with self._lock:
                self.stats["restarts"] += 1
        except Exception:
            log.exception("camera restart failed")
            self._stop.wait(1.0)

    # -------------------------------------------------------------- fan-out
    def subscribe(self, loop: asyncio.AbstractEventLoop,
                  maxsize: int = 8) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers = [(l, q) for l, q in self._subscribers if q is not queue]

    @property
    def subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def _publish(self, sample: FocusSample) -> None:
        with self._sub_lock:
            subscribers = list(self._subscribers)
        if not subscribers:
            return
        payload = sample.to_dict()
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, payload)
            except RuntimeError:
                # Loop already closed; the WS handler will clean itself up.
                pass

    @staticmethod
    def _offer(queue: asyncio.Queue, payload: Dict[str, Any]) -> None:
        """Enqueue, dropping the oldest sample if the client is behind."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    # ---------------------------------------------------------------- readers
    @property
    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._latest.to_dict() if self._latest else None

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    @property
    def latest_projection(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest_projection is None else self._latest_projection.copy()

    def snapshot(self) -> Tuple[Optional[np.ndarray], Optional[np.ndarray],
                                Optional[Dict[str, Any]]]:
        """Frame, projection and sample from the *same* iteration."""
        with self._lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
            proj = None if self._latest_projection is None else self._latest_projection.copy()
            sample = self._latest.to_dict() if self._latest else None
        return frame, proj, sample

    def history(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)[-int(limit):]

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def wait_for_fresh_sample(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Block until a sample newer than the current one arrives.

        Used after moving the simulated stage so the caller reads pixels that
        were actually exposed at the new position, not the frame in flight.
        """
        with self._lock:
            start_seq = self._latest.seq if self._latest else -1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest and self._latest.seq > start_seq + 1:
                    return self._latest.to_dict()
            time.sleep(0.001)
        return self.latest

    # ------------------------------------------------------------- parameters
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            stats = copy.deepcopy(self.stats)
        stats["uptime_s"] = round(time.time() - stats["started_at"], 1) \
            if stats["started_at"] else 0.0
        return {
            "running": self.running,
            "camera": self.camera.get_params(),
            "focus_params": self.estimator.params.to_dict(),
            "stats": stats,
            "subscribers": self.subscriber_count,
        }

    def set_camera_params(self, **kwargs) -> Dict[str, Any]:
        with self._lock:
            return self.camera.set_params(**kwargs)

    def set_focus_params(self, **kwargs) -> Dict[str, Any]:
        with self._lock:
            self.estimator.update_params(**kwargs)
            self.estimator.reset_history()
            return self.estimator.params.to_dict()
