#!/usr/bin/env python3
"""Run the focus sensor client straight out of a checkout, uninstalled.

The client itself lives in ``focussensor.client`` and is installed as the
``focussensor-client`` command. This wrapper exists so the tool also works on a
machine where nothing has been installed — copy the repo onto a laptop, run it,
talk to a sensor.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "software"))

from focussensor.client import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
