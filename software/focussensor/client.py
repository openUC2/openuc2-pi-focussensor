"""Standalone client for the openUC2 Pi focus sensor — no ImSwitch involved.

This is the tool for bringing a sensor up: point it at the Pi, check the sensor
is alive, tune exposure and thresholds, watch the focus value stream, and run a
z sweep to measure the triangulation sensitivity that the focus lock will need.

    focussensor-client --host focussensor.local status
    focussensor-client set --exposure-us 8000 --gain 2.0
    focussensor-client set --roi 408 444 640 200 --sigma 3.5
    focussensor-client watch --seconds 5
    focussensor-client sweep --start -15 --stop 15 --steps 31
    focussensor-client snap --out /tmp/focus.jpg

Without installing the package, ``tools/focussensor_client.py`` runs the same
thing straight out of a checkout.

``watch`` uses the WebSocket when the ``websockets`` package is available and
falls back to REST polling otherwise, so the script runs on a bare Python.

Only the standard library is required for everything except ``watch``, which
uses the WebSocket when ``websockets`` is importable and polls REST otherwise.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class FocusSensorClient:
    """Thin wrapper over the sensor's REST API."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8321, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.host, self.port, self.timeout = host, port, timeout

    # ----------------------------------------------------------------- plumbing
    def _request(self, method: str, path: str, payload: Any = None,
                 raw: bool = False) -> Any:
        url = self.base + path
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc
        return body if raw else json.loads(body)

    def get(self, path: str, **params) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        return self._request("GET", path)

    def post(self, path: str, payload: Any = None) -> Any:
        return self._request("POST", path, payload if payload is not None else {})

    # -------------------------------------------------------------------- API
    def status(self) -> Dict[str, Any]:
        return self.get("/api/status")

    def focus(self) -> Dict[str, Any]:
        return self.get("/api/focus")

    def history(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.get("/api/focus/history", limit=limit)["samples"]

    def camera_params(self, **params) -> Dict[str, Any]:
        return self.post("/api/camera/params", params) if params \
            else self.get("/api/camera/params")

    def focus_params(self, **params) -> Dict[str, Any]:
        return self.post("/api/focus/params", params) if params \
            else self.get("/api/focus/params")

    def limits(self) -> Dict[str, Any]:
        return self.get("/api/camera/limits")

    def frame_jpeg(self, overlay: bool = True, scale: float = 1.0) -> bytes:
        query = urllib.parse.urlencode({"overlay": str(overlay).lower(), "scale": scale})
        return self._request("GET", f"/api/frame.jpg?{query}", raw=True)

    def save_params(self) -> Dict[str, Any]:
        return self.post("/api/params/save", {})

    def sim_state(self) -> Dict[str, Any]:
        return self.get("/api/sim/state")

    def sim_set(self, z_um: Optional[float] = None, wait: bool = True,
                **params) -> Dict[str, Any]:
        """Move the simulated stage and get the resulting sample back."""
        body: Dict[str, Any] = {"wait": wait, **params}
        if z_um is not None:
            body["z_um"] = z_um
        return self.post("/api/sim/state", body)

    # ---------------------------------------------------------------- measure
    def sweep(self, start: float, stop: float, steps: int,
              settle_s: float = 0.05, averages: int = 3
              ) -> List[Tuple[float, Optional[float], Optional[float], float]]:
        """Step the simulated stage and record focus at each position.

        Returns ``(z_um, focus_px, separation_px, quality)`` per step. This is
        the same measurement ImSwitch's calibration performs against a real
        stage, run standalone so the optical model can be checked in isolation.
        """
        if not self.status().get("simulated"):
            raise RuntimeError(
                "sweep drives the simulated stage; this sensor has a real camera. "
                "Move the real stage and read /api/focus instead.")
        rows = []
        for i in range(steps):
            z = start + (stop - start) * i / max(1, steps - 1)
            self.sim_set(z_um=z)
            if settle_s:
                time.sleep(settle_s)
            samples = []
            for _ in range(max(1, averages)):
                sample = self.focus()
                if sample.get("valid"):
                    samples.append(sample)
                time.sleep(0.01)
            if samples:
                focus = sum(s["focus"] for s in samples) / len(samples)
                seps = [s["x_peak_distance"] for s in samples
                        if s.get("x_peak_distance") is not None]
                separation = sum(seps) / len(seps) if seps else None
                quality = sum(s["quality"] for s in samples) / len(samples)
                rows.append((z, focus, separation, quality))
            else:
                rows.append((z, None, None, 0.0))
        return rows


# --------------------------------------------------------------------- helpers
def _linear_fit(xs: List[float], ys: List[float]) -> Tuple[float, float, float]:
    """Least-squares slope, intercept and R^2 — no numpy needed."""
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 1.0
    return slope, intercept, r2


def _sparkline(values: List[float], width: int = 60) -> str:
    """ASCII plot so a sweep is readable over SSH without a plotting stack."""
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = max(1, len(values) // width)
    return "".join(blocks[min(7, int((v - lo) / span * 7.999))]
                   for v in values[::step])


# -------------------------------------------------------------------- commands
def cmd_status(client: FocusSensorClient, args) -> int:
    status = client.status()
    camera, stats = status["camera"], status["stats"]
    print(f"{status['name']}  @ {client.base}")
    print(f"  camera     : {camera['model']} "
          f"({'SIMULATED' if status['simulated'] else 'hardware'})")
    print(f"  running    : {status['running']}   subscribers: {status['subscribers']}")
    print(f"  frame      : {camera['frame_shape']['width']}x"
          f"{camera['frame_shape']['height']} "
          f"roi={camera['roi']}  bin={camera['binning']}")
    print(f"  exposure   : {camera['exposure_us']} us   gain: {camera['gain']}")
    print(f"  rate       : {stats['fps']} Hz "
          f"(target {camera['fps_target']}, achievable {camera['achievable_fps']})")
    print(f"  compute    : {stats['compute_ms']} ms/frame")
    print(f"  frames     : {stats['frames']}  valid: {stats['valid']}  "
          f"errors: {stats['errors']}  restarts: {stats['restarts']}")
    if stats.get("last_error"):
        print(f"  last error : {stats['last_error']}")
    try:
        sample = client.focus()
        print(f"  focus      : {sample['focus']:.3f} px  "
              f"sep {sample['x_peak_distance']:.2f} px  q {sample['quality']:.1f}  "
              f"valid={sample['valid']}  age {sample['age_s']*1000:.1f} ms")
    except Exception as exc:
        print(f"  focus      : unavailable ({exc})")
    return 0


def cmd_params(client: FocusSensorClient, args) -> int:
    print("camera:"), print(json.dumps(client.camera_params(), indent=2))
    print("limits:"), print(json.dumps(client.limits(), indent=2))
    print("focus:"), print(json.dumps(client.focus_params(), indent=2))
    return 0


def cmd_set(client: FocusSensorClient, args) -> int:
    camera: Dict[str, Any] = {}
    if args.exposure_us is not None:
        camera["exposure_us"] = args.exposure_us
    if args.gain is not None:
        camera["gain"] = args.gain
    if args.fps is not None:
        camera["fps_target"] = args.fps
    if args.binning is not None:
        camera["binning"] = args.binning
    if args.roi:
        x, y, w, h = args.roi
        camera["roi"] = {"x": x, "y": y, "width": w, "height": h}
    if camera:
        print("camera ->", json.dumps(client.camera_params(**camera), indent=2))

    focus: Dict[str, Any] = {}
    if args.sigma is not None:
        focus["gaussian_sigma"] = args.sigma
    if args.peak_distance is not None:
        focus["peak_distance"] = args.peak_distance
    if args.prominence is not None:
        focus["peak_prominence_mad"] = args.prominence
    if args.min_quality is not None:
        focus["min_quality"] = args.min_quality
    if focus:
        print("focus ->", json.dumps(client.focus_params(**focus), indent=2))

    if args.save:
        print("saved ->", json.dumps(client.save_params(), indent=2))
    if not camera and not focus and not args.save:
        print("nothing to set; see --help", file=sys.stderr)
        return 2
    return 0


def cmd_watch(client: FocusSensorClient, args) -> int:
    """Stream focus values, over the socket when possible."""
    deadline = time.monotonic() + args.seconds if args.seconds else None
    count = 0
    t_start = time.monotonic()

    def show(sample: Dict[str, Any]) -> None:
        nonlocal count
        count += 1
        if args.json:
            print(json.dumps(sample), flush=True)
            return
        focus = "  none  " if sample.get("focus") is None else f"{sample['focus']:8.3f}"
        sep = "   none" if sample.get("x_peak_distance") is None \
            else f"{sample['x_peak_distance']:7.2f}"
        z = "" if sample.get("z_um") is None else f"  z={sample['z_um']:8.3f}"
        flag = "" if sample.get("valid") else "  INVALID"
        rate = count / max(1e-6, time.monotonic() - t_start)
        print(f"seq {sample['seq']:>7}  focus {focus}  sep {sep}  "
              f"q {sample['quality']:6.1f}  {rate:6.1f} Hz{z}{flag}", flush=True)

    if not args.poll:
        try:
            return _watch_ws(client, args, show, deadline)
        except ImportError:
            print("# websockets not installed, falling back to REST polling",
                  file=sys.stderr)

    period = 1.0 / max(0.1, args.rate)
    last_seq = -1
    while deadline is None or time.monotonic() < deadline:
        try:
            sample = client.focus()
            if sample["seq"] != last_seq:
                last_seq = sample["seq"]
                show(sample)
        except RuntimeError as exc:
            print(f"# {exc}", file=sys.stderr)
        time.sleep(period)
    return 0


def _watch_ws(client: FocusSensorClient, args, show, deadline) -> int:
    import asyncio

    import websockets      # raises ImportError -> caller falls back to polling

    url = f"ws://{client.host}:{client.port}/ws/focus?decimate={args.decimate}"

    async def run() -> None:
        async with websockets.connect(url, max_queue=8) as socket:
            while deadline is None or time.monotonic() < deadline:
                timeout = None if deadline is None else max(
                    0.01, deadline - time.monotonic())
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    return
                show(json.loads(message))

    asyncio.run(run())
    return 0


def cmd_sweep(client: FocusSensorClient, args) -> int:
    """Measure focus vs z — the calibration ImSwitch will later do itself."""
    print(f"sweeping z from {args.start} to {args.stop} um in {args.steps} steps")
    rows = client.sweep(args.start, args.stop, args.steps,
                        settle_s=args.settle, averages=args.averages)

    print(f"\n{'z (um)':>9} {'focus (px)':>12} {'sep (px)':>10} {'quality':>9}")
    for z, focus, separation, quality in rows:
        focus_s = "     none" if focus is None else f"{focus:12.3f}"
        sep_s = "      none" if separation is None else f"{separation:10.2f}"
        print(f"{z:9.3f} {focus_s} {sep_s} {quality:9.1f}")

    valid = [(z, f) for z, f, _, _ in rows if f is not None]
    if len(valid) < 3:
        print("\nnot enough valid points to fit", file=sys.stderr)
        return 1

    print("\nfocus vs z: " + _sparkline([f for _, f in valid]))

    # Fit the middle half, where the response is linear — the outer points of a
    # real sweep run out of capture range and would drag the slope down.
    lo, hi = len(valid) // 4, len(valid) - len(valid) // 4
    core = valid[lo:hi] if hi - lo >= 3 else valid
    slope, intercept, r2 = _linear_fit([z for z, _ in core], [f for _, f in core])
    print(f"\nlinear fit over z in [{core[0][0]:.2f}, {core[-1][0]:.2f}] um:")
    print(f"  sensitivity : {slope:.4f} px/um   ({1/slope if slope else float('nan'):.4f} um/px)")
    print(f"  intercept   : {intercept:.3f} px")
    print(f"  R^2         : {r2:.5f}")
    print("\nUse the inverse as scaleUmPerUnit in the ImSwitch focusLock config,")
    print("or let ImSwitch's own calibration measure it against the real stage.")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as handle:
            handle.write("z_um,focus_px,separation_px,quality\n")
            for z, focus, separation, quality in rows:
                handle.write(f"{z},{focus if focus is not None else ''},"
                             f"{separation if separation is not None else ''},{quality}\n")
        print(f"\nwrote {args.csv}")
    return 0


def cmd_snap(client: FocusSensorClient, args) -> int:
    data = client.frame_jpeg(overlay=not args.no_overlay, scale=args.scale)
    with open(args.out, "wb") as handle:
        handle.write(data)
    print(f"wrote {args.out} ({len(data)} bytes)")
    return 0


def cmd_simz(client: FocusSensorClient, args) -> int:
    result = client.sim_set(z_um=args.z_um)
    sample = result.get("sample") or {}
    print(f"z = {result['z_um']:.3f} um -> focus "
          f"{sample.get('focus') if sample.get('focus') is None else round(sample['focus'], 3)} px"
          f"  valid={sample.get('valid')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="sensor hostname or IP (default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8321)
    parser.add_argument("--timeout", type=float, default=5.0)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one-screen summary of the sensor").set_defaults(
        func=cmd_status)
    sub.add_parser("params", help="dump camera limits and all parameters").set_defaults(
        func=cmd_params)

    p_set = sub.add_parser("set", help="change camera and estimator parameters")
    p_set.add_argument("--exposure-us", type=int)
    p_set.add_argument("--gain", type=float)
    p_set.add_argument("--fps", type=float)
    p_set.add_argument("--binning", type=int)
    p_set.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "W", "H"))
    p_set.add_argument("--sigma", type=float, help="estimator gaussian_sigma")
    p_set.add_argument("--peak-distance", type=int)
    p_set.add_argument("--prominence", type=float, help="peak_prominence_mad")
    p_set.add_argument("--min-quality", type=float)
    p_set.add_argument("--save", action="store_true",
                       help="persist the running values to the config file")
    p_set.set_defaults(func=cmd_set)

    p_watch = sub.add_parser("watch", help="stream focus values")
    p_watch.add_argument("--seconds", type=float, default=0,
                         help="stop after N seconds (0 = forever)")
    p_watch.add_argument("--decimate", type=int, default=1,
                         help="show every Nth sample (websocket only)")
    p_watch.add_argument("--poll", action="store_true",
                         help="force REST polling instead of the websocket")
    p_watch.add_argument("--rate", type=float, default=20.0, help="poll rate in Hz")
    p_watch.add_argument("--json", action="store_true", help="one JSON object per line")
    p_watch.set_defaults(func=cmd_watch)

    p_sweep = sub.add_parser("sweep", help="z sweep and sensitivity fit (simulation)")
    p_sweep.add_argument("--start", type=float, default=-10.0)
    p_sweep.add_argument("--stop", type=float, default=10.0)
    p_sweep.add_argument("--steps", type=int, default=21)
    p_sweep.add_argument("--settle", type=float, default=0.05)
    p_sweep.add_argument("--averages", type=int, default=3)
    p_sweep.add_argument("--csv", help="also write the sweep to this CSV file")
    p_sweep.set_defaults(func=cmd_sweep)

    p_snap = sub.add_parser("snap", help="save the annotated debug frame")
    p_snap.add_argument("--out", default="focus_frame.jpg")
    p_snap.add_argument("--no-overlay", action="store_true")
    p_snap.add_argument("--scale", type=float, default=1.0)
    p_snap.set_defaults(func=cmd_snap)

    p_simz = sub.add_parser("simz", help="move the simulated stage")
    p_simz.add_argument("z_um", type=float)
    p_simz.set_defaults(func=cmd_simz)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = FocusSensorClient(args.host, args.port, args.timeout)
    try:
        return args.func(client, args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
