#!/usr/bin/env python3
"""Debug entry point for the focus sensor.

Runs straight out of a checkout — no install, no ``PYTHONPATH``, no systemd —
so you can set a breakpoint anywhere and step through it:

    python main.py                 # serve, simulated camera, debug logging
    python main.py --backend auto  # serve, real Pi camera if one is present
    python main.py selftest        # headless z sweep, no HTTP at all
    python main.py snap            # one annotated frame to a file

``selftest`` is the one to reach for when something is wrong. It builds the
engine in this process, sweeps the simulated stage, and prints focus against z
with a fitted sensitivity — everything the sensor does, in a single stack you
can step through, with no server, no sockets and no ImSwitch in the way.

For the real service use the ``focussensor`` command (see pyproject.toml);
this file is for poking at it.
"""

import argparse
import logging
import pathlib
import sys
import time

# Work uninstalled: put ``software/`` on the path before importing the package.
REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "software"))

from focussensor.config import load_config                    # noqa: E402
from focussensor.engine import FocusEngine                    # noqa: E402
from focussensor.focus import FocusParams                     # noqa: E402


def _build_engine(args) -> FocusEngine:
    config = load_config(args.config)
    camera = config.setdefault("camera", {})
    if args.backend:
        camera["backend"] = args.backend
    if args.fps:
        camera.setdefault("startup", {})["fps_target"] = args.fps
    print(f"config   : {config.get('_path') or 'built-in defaults'}")
    print(f"backend  : {camera.get('backend')}")
    engine = FocusEngine(camera, FocusParams(**config.get("focus", {})))
    print(f"camera   : {engine.camera.model}"
          f"{'  (SIMULATED)' if engine.camera.simulated else ''}")
    params = engine.camera.get_params()
    print(f"roi      : {params['roi']}  ->  frame "
          f"{params['frame_shape']['width']}x{params['frame_shape']['height']}"
          f"  bin {params['binning']}")
    print(f"exposure : {params['exposure_us']} us   gain {params['gain']}   "
          f"fps target {params['fps_target']}")
    return engine


def cmd_serve(args) -> int:
    """Run the real service, with debug logging and the simulator by default."""
    import uvicorn

    from focussensor.server import create_app

    config = load_config(args.config)
    server = config.setdefault("server", {})
    if args.backend:
        config.setdefault("camera", {})["backend"] = args.backend
    if args.port:
        server["port"] = args.port
    host, port = server.get("host", "0.0.0.0"), int(server.get("port", 8321))

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"config    : {config.get('_path') or 'built-in defaults'}")
    print(f"backend   : {config.get('camera', {}).get('backend')}")
    print(f"debug view: http://{shown}:{port}/")
    print(f"REST docs : http://{shown}:{port}/docs")
    print(f"socket    : ws://{shown}:{port}/ws/focus")
    print()
    uvicorn.run(create_app(config), host=host, port=port, log_level="debug")
    return 0


def cmd_selftest(args) -> int:
    """Sweep the simulated stage and check the focus response, no HTTP."""
    engine = _build_engine(args)
    if not engine.camera.simulated:
        print("\nselftest drives the simulated stage; this build has a real "
              "camera. Use --backend simulated.", file=sys.stderr)
        return 2

    engine.start()
    try:
        if engine.wait_for_fresh_sample(timeout=5.0) is None:
            print("\nno samples arrived within 5 s", file=sys.stderr)
            return 1

        def column(value, width, decimals):
            """Fixed-width cell that also renders a missing measurement."""
            return f"{value:{width}.{decimals}f}" if value is not None \
                else "none".rjust(width)

        print(f"\n{'z (um)':>9} {'focus (px)':>12} {'sep (px)':>10} "
              f"{'quality':>9} {'ms':>7}  valid")
        rows = []
        for i in range(args.steps):
            z = args.start + (args.stop - args.start) * i / max(1, args.steps - 1)
            engine.camera.set_z(z)
            sample = engine.wait_for_fresh_sample(timeout=2.0) or {}
            focus = sample.get("focus")
            if focus is not None:
                rows.append((z, focus))
            print(f"{z:9.3f} {column(focus, 12, 3)} "
                  f"{column(sample.get('x_peak_distance'), 10, 2)} "
                  f"{sample.get('quality', 0.0):9.1f} "
                  f"{sample.get('compute_ms', 0.0):7.2f}  {sample.get('valid')}")

        if len(rows) < 3:
            print("\ntoo few valid points to fit — check the ROI and thresholds",
                  file=sys.stderr)
            return 1

        # Fit the middle half: the ends of a wide sweep leave the linear range.
        lo, hi = len(rows) // 4, len(rows) - len(rows) // 4
        core = rows[lo:hi] if hi - lo >= 3 else rows
        n = len(core)
        mx = sum(z for z, _ in core) / n
        my = sum(f for _, f in core) / n
        sxx = sum((z - mx) ** 2 for z, _ in core)
        slope = sum((z - mx) * (f - my) for z, f in core) / sxx if sxx else 0.0

        stats = engine.get_status()["stats"]
        print(f"\nsensitivity over z in [{core[0][0]:.2f}, {core[-1][0]:.2f}] um: "
              f"{slope:.4f} px/um")
        print(f"rate {stats['fps']} Hz, {stats['compute_ms']} ms/frame, "
              f"{stats['frames']} frames, {stats['errors']} errors")
        if abs(slope) < 0.1:
            print("\nthe focus value barely moves with z — the spots are probably "
                  "outside the ROI", file=sys.stderr)
            return 1
        print("\nselftest OK")
        return 0
    finally:
        engine.close()


def cmd_snap(args) -> int:
    """Save one annotated frame — the fastest way to see what the estimator sees."""
    from focussensor import overlay

    engine = _build_engine(args)
    engine.start()
    try:
        # Wait for the loop to be producing before moving: on a cold engine the
        # first sample is frame #1, which predates the move and would show the
        # old position.
        if engine.wait_for_fresh_sample(timeout=5.0) is None:
            print("\nno frame arrived within 5 s", file=sys.stderr)
            return 1
        if args.z is not None and engine.camera.simulated:
            engine.camera.set_z(args.z)
            sample = engine.wait_for_fresh_sample(timeout=5.0)
        else:
            sample = engine.latest
        if sample is None:
            print("\nno frame arrived within 5 s", file=sys.stderr)
            return 1
        frame, projection, snap = engine.snapshot()
        pathlib.Path(args.out).write_bytes(
            overlay.render(frame, projection, snap, scale=args.scale))
        print(f"\nfocus {snap.get('focus')}  sep {snap.get('x_peak_distance')}  "
              f"quality {snap.get('quality'):.1f}  valid {snap.get('valid')}")
        print(f"wrote {args.out}")
        return 0
    finally:
        engine.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(REPO / "config" / "focussensor.yaml"))
    parser.add_argument("--backend", default="simulated",
                        choices=["auto", "picamera2", "simulated"],
                        help="default: simulated, so this works anywhere")
    parser.add_argument("--fps", type=float, help="override the target frame rate")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG level logging")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the service (default)")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_test = sub.add_parser("selftest", help="headless z sweep, no HTTP")
    p_test.add_argument("--start", type=float, default=-10.0)
    p_test.add_argument("--stop", type=float, default=10.0)
    p_test.add_argument("--steps", type=int, default=11)
    p_test.set_defaults(func=cmd_selftest)

    p_snap = sub.add_parser("snap", help="save one annotated frame")
    p_snap.add_argument("--out", default="focus_debug.jpg")
    p_snap.add_argument("--z", type=float, help="simulated stage position first")
    p_snap.add_argument("--scale", type=float, default=1.0)
    p_snap.set_defaults(func=cmd_snap)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or sys.argv[1:]) + ["serve"])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
