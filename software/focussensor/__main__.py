"""Entry point: ``python -m focussensor [--config PATH]``."""

import argparse
import logging
import sys

from .config import load_config
from .server import create_app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="focussensor",
                                     description="openUC2 Pi focus sensor service")
    parser.add_argument("--config", default=None,
                        help="path to focussensor.yaml (default: search the boot "
                             "partition, then ./config)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--backend", default=None,
                        choices=["auto", "picamera2", "simulated"],
                        help="override camera.backend from the config file")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    server = config.setdefault("server", {})
    if args.host:
        server["host"] = args.host
    if args.port:
        server["port"] = args.port
    if args.log_level:
        server["log_level"] = args.log_level
    if args.backend:
        config.setdefault("camera", {})["backend"] = args.backend

    logging.basicConfig(
        level=getattr(logging, str(server.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    import uvicorn
    uvicorn.run(create_app(config),
                host=server.get("host", "0.0.0.0"),
                port=int(server.get("port", 8321)),
                log_level=str(server.get("log_level", "info")).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
