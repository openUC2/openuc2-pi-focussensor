# openUC2 Pi focus sensor

A **satellite focus sensor**: a Raspberry Pi Zero 2 W with a Pi camera that
owns the two-spot reflection optics, does the peak estimation on its own CPU,
and reports a single focus value to ImSwitch over one USB cable.

The point is to get the second camera off the microscope host. Today a focus
lock costs an industrial USB3 camera, a USB3 port, its bandwidth, and a share
of the same CPU that is running the imaging camera — to produce one float
several times a second. Here the Pi does all of that and the host reads a
number.

```
  laser ──► sample ──► two reflections
                          │
                    ┌─────▼──────────────────────────┐
                    │ Pi Zero 2 W                    │
                    │  camera ─► ROI ─► peak fit     │   ~1 ms/frame
                    └─────┬──────────────────────────┘
                          │  one USB cable: power + CDC-NCM ethernet
                    ┌─────▼──────────────────────────┐
                    │ ImSwitch host                  │
                    │  RemoteFocusSensorManager       │
                    │  FocusLockController: PI ─► Z   │
                    └────────────────────────────────┘
```

**Everything runs in simulation first.** The default camera backend renders a
physically-shaped two-spot image whose peaks translate in x with z, so the
complete ImSwitch focus lock — live value, calibration sweep, PI lock — can be
brought up and debugged before any optics exist.

## Quick start (no hardware)

```bash
pip install numpy pillow pyyaml "fastapi>=0.110" "uvicorn[standard]>=0.27" "websockets>=12"
PYTHONPATH=software python -m focussensor --config config/focussensor.yaml --backend simulated
```

Then, from anywhere:

```bash
tools/focussensor_client.py status
tools/focussensor_client.py watch --seconds 5
tools/focussensor_client.py sweep --start -10 --stop 10 --steps 21
```

The sweep drives the simulated stage and fits the triangulation sensitivity —
the same measurement ImSwitch's calibration performs against a real stage:

```
linear fit over z in [-6.00, 6.00] um:
  sensitivity : 2.9478 px/um   (0.3392 um/px)
  R^2         : 0.99973
```

Open <http://127.0.0.1:8321/> for the live alignment view: the ROI with the
detected peaks and the projection drawn on it, plus the running focus value.

## Why USB ethernet and not UVC

A composite USB gadget can absolutely carry UVC video *and* a data channel on
one cable — `f_uvc` alongside `f_ncm` in configfs is a normal thing to build.
It is still the wrong choice here:

* The sensor's real output is one number, not video. UVC gets you a video
  stream that ImSwitch does not consume, and the debug image HTTP already
  serves for free.
* `uvc-gadget` on a Pi is the most fragile part of that stack. CDC-NCM is a
  stock kernel function with no userspace daemon to wedge.
* NCM works driverless on Linux and macOS and with the in-box driver on
  Windows 10+, and carries REST, the WebSocket and the MJPEG debug view over
  one interface.

CAN was the other candidate. It suits the *payload* fine (a float32 plus status
is five bytes), but it carries no image, so alignment would need a second
channel anyway; the Pi has no CAN controller without extra hardware; and adding
a fast focus stream to the bus that already carries motion commands is how you
make stage moves jittery. CAN earns its place in a later step — see
[Where the loop lives](#where-the-loop-lives).

## Wire protocol

Timestamps are **seconds** (float). `t` is wall-clock, `t_mono` is monotonic
and is the one to use for rate and staleness maths.

### WebSocket — the fast path

`ws://<sensor>:8321/ws/focus` pushes one JSON object per frame:

```json
{"seq": 12345, "t": 1788347395.92, "t_mono": 384156.76, "valid": true,
 "focus": 210.21, "left_peak_x": 210.21, "right_peak_x": 430.44,
 "x_peak_distance": 220.23, "avg_peak_distance": 220.03, "n_peaks": 2,
 "quality": 162.5, "saturated_fraction": 0.0, "compute_ms": 0.74, "z_um": 0.09}
```

`?decimate=N` sends every Nth sample. Backlog is dropped rather than queued: a
client that stalls resumes on the newest data, because for a focus lock a
sample from 20 ms ago is worthless.

### REST — setup and debugging

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | camera, estimator, rate, error and restart counts |
| `GET /api/health` | liveness plus sample age |
| `GET /api/focus` | latest sample (poll this if you cannot use the socket) |
| `GET /api/focus/history?limit=N` | last N samples, enough to plot a sweep |
| `GET /api/focus/projection` | the smoothed projection the peaks were found in |
| `GET`/`POST /api/focus/params` | estimator parameters |
| `POST /api/focus/reset` | forget the running separation history |
| `GET`/`POST /api/camera/params` | exposure, gain, binning, fps, ROI |
| `GET /api/camera/limits` | what the backend will actually accept |
| `GET /api/frame.jpg` | annotated ROI — the alignment view |
| `GET /api/frame.npy` | raw ROI frame, what ImSwitch reads |
| `GET /api/stream.mjpg` | Motion-JPEG of the annotated ROI |
| `GET`/`POST /api/sim/state` | drive the simulated stage and optical model |
| `POST /api/params/save` | persist running settings to the YAML |

Interactive docs at `http://<sensor>:8321/docs`.

## ImSwitch integration

Add a detector using `RemoteFocusSensorManager` and point `focusLock.camera` at
it. A ready-made, fully simulated setup ships with ImSwitch as
`example_remote_focussensor.json`:

```json
"FocusSensor": {
  "managerName": "RemoteFocusSensorManager",
  "managerProperties": {
    "host": "192.168.7.2",
    "port": 8321,
    "useWebsocket": true,
    "maxSampleAgeMs": 500,
    "cameraParams": {"exposure_us": 5000, "gain": 1.0,
                     "roi": {"x": 408, "y": 444, "width": 640, "height": 200}},
    "focusParams": {"gaussian_sigma": 3.0, "peak_distance": 40, "min_quality": 10.0}
  },
  "forAcquisition": false,
  "forFocusLock": true
}
```

Nothing else in ImSwitch changes. `FocusLockController` keeps the PI loop, the
calibration, the CSV log and the whole REST API; the one difference is that
when a detector offers `getFocusValue()` the controller reads the sensor's
estimate instead of computing its own. Cropping, exposure and gain map onto the
sensor's REST API, so the existing widgets keep working, and
`getLatestFrame()` still returns real pixels for the alignment view.

Two behaviours are worth knowing about:

* **Staleness.** A sample older than `maxSampleAgeMs` is treated as a failed
  measurement, not a valid one. A frozen link must never feed the PI loop the
  same value forever.
* **Simulation.** When the sensor reports itself as simulated, the controller
  mirrors the current stage Z into it before each read and gets back the sample
  exposed *after* that move, in one round trip. That is what makes an
  end-to-end simulated focus lock — including a calibration sweep — possible.

## Hardware

* **Raspberry Pi Zero 2 W**, not the original Zero. libcamera plus a per-frame
  estimator on a single ARMv6 core is not a good time; the quad A53 has
  headroom to spare.
* **Global shutter (IMX296)** if there is vibration or the spot moves during
  the exposure. IMX219 is fine and cheaper otherwise.
* One USB cable into the Pi's **OTG** port carries both power and the link.

The single most important setting is the **ROI**. Configure a narrow band
around the two spots (e.g. 640×200) so the sensor delivers those pixels
directly at a high frame rate. Grabbing full frames and cropping in numpy is
what would make a Pi struggle; the arithmetic never was the problem.

## Installing on a Pi

```bash
git clone https://github.com/openUC2/openuc2-pi-focussensor
sudo bash openuc2-pi-focussensor/sd-image/install-focussensor.sh
sudo reboot
```

The installer is idempotent — run it again after a `git pull` to update.

It installs the service to `/opt/focussensor` with a venv (numpy, Pillow and
picamera2 come from apt; building them with pip on a Pi is slow at best),
enables `focussensor.service`, brings up the CDC-NCM USB gadget with a static
`192.168.7.2` on `usb0`, and sets the hostname to `focussensor.local` so the
host can reach it by name without configuring an address of its own.

Config lives at `/boot/firmware/focussensor.yaml`, editable with the card in a
laptop.

### Pre-built SD image

Tagging `v*` (or running the workflow by hand) builds a flashable image in
GitHub Actions: the official Bookworm arm64 Lite image is chrooted with qemu
and the same installer is run inside it, so the image and a hand-installed Pi
are identical. The result is attached to a GitHub release.

## Simulation model

The simulated camera is not a placeholder — it reproduces the behaviour that
makes a focus lock hard:

* both spots translate in x at `sensitivity_px_per_um`, linear near focus and
  saturating via `tanh` beyond `capture_range_um`, because a real spot walks
  off the detector rather than travelling forever;
* their separation grows at `separation_px_per_um`, since the two interfaces
  sit at different heights — this reproduces the slow `x_peak_distance` drift a
  real system shows;
* spots blur with defocus and dim as they blur, conserving energy, so signal
  quality genuinely degrades away from focus and the capture range is finite;
* brightness scales with exposure and gain, with Poisson shot noise, Gaussian
  read noise, a stray-light gradient and clipping at the bit depth;
* a slow thermal drift plus per-frame jitter give an idle lock something to
  fight, and make a released lock visibly wander off.

`dropout_probability` and `hot_pixel_fraction` are there to exercise the
invalid-sample and spike-rejection paths.

## Estimator

A port of `PeakMetric` from ImSwitch's `focusmetrics.py` — same pipeline, same
output keys, so moving the computation onto the sensor does not change what
ImSwitch sees:

```
x-projection → baseline removal → Gaussian smoothing → MAD scaling
  → peak finding → two strongest peaks → parabolic subpixel
```

The focus value is the subpixel x of the **left** spot. The two SciPy
functions this needs (`gaussian_filter1d`, `find_peaks`) are reimplemented in
pure numpy in `dsp.py`, so a Pi image does not carry SciPy for eighty lines of
code. The test suite asserts they still match SciPy exactly.

`min_quality` is the guard that matters: MAD scaling means noise still produces
"peaks". Measured, pure sensor noise through this pipeline peaks at ~6 MAD
while a real two-spot frame reaches several hundred, so the shipped default of
10 rejects noise with margin and never touches a real signal. Without it a
blocked beam feeds noise-derived positions straight into the PI loop.

## Where the loop lives

Today the host keeps the PI loop: the sensor reports, `FocusLockController`
decides and moves Z. That is a drop-in change and already removes the camera,
its bandwidth and the per-frame maths from the host.

The next step, once the sensor path is proven on hardware, is to move the PI
loop across too — the sensor nudges Z directly over CAN, where the UC2 board
already lives, and ImSwitch becomes supervisory (arm/disarm, setpoint, state).
That removes Python, the GIL and host scheduling from the control loop
entirely. The wire protocol here is deliberately shaped so that step does not
require redesigning it.

The hazard to design for then is **two masters on Z**. If the sensor closes the
loop it must issue only bounded relative nudges, only while ImSwitch has armed
it, ImSwitch must be able to disarm instantly, and the accumulated offset must
be reported back so host-side absolute Z bookkeeping does not drift. The
cleanest variant puts the PI in the UC2 firmware and has the sensor publish
only a focus *error*, leaving the motion controller the sole owner of the
stage.

## Repository layout

```
software/focussensor/       the service
  cameras/base.py           camera interface (exposure, gain, ROI, binning, fps)
  cameras/simulated.py      two-spot optical + noise model
  cameras/picamera2_backend.py  real Pi camera (untested on hardware)
  dsp.py                    pure-numpy gaussian_filter1d and find_peaks
  focus.py                  the two-spot peak estimator
  engine.py                 acquisition loop, fan-out, watchdog
  server.py                 REST + WebSocket + MJPEG + debug page
  overlay.py                annotated debug JPEG
tools/focussensor_client.py standalone client: status, set, watch, sweep, snap
config/focussensor.yaml     shipped defaults
sd-image/                   installer and USB gadget setup
tests/                      hardware-free test suite
```

## Status

The simulated path is tested end to end, including through ImSwitch's focus
lock. **The Picamera2 backend has not yet been run on hardware** — it is
written against the same interface and follows libcamera's rules for a
metrology sensor (all auto algorithms off, `ScalerCrop` for the ROI, luma plane
only, frame duration pinned), but treat its first run as bring-up.

## Licence

Not yet chosen — add one before publishing.
