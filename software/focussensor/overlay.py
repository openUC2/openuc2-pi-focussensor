"""Debug image rendering.

Turns the ROI frame plus the estimator's projection into an annotated JPEG.
This is the whole reason for keeping a video path on a sensor whose real
output is one float: aligning the optics and choosing the ROI is a visual job,
and being able to point a browser at the sensor and *see* which peaks it is
locking onto is worth far more than reading numbers.

It costs nothing when nobody is looking — the overlay is rendered on request,
not in the acquisition loop.
"""

import io
from typing import Any, Dict, Optional

import numpy as np

try:
    from PIL import Image, ImageDraw
    _HAVE_PIL = True
except Exception:                                   # pragma: no cover
    _HAVE_PIL = False


def _stretch(frame: np.ndarray, low_pct: float = 0.5, high_pct: float = 99.9) -> np.ndarray:
    """Percentile contrast stretch — a dark ROI with two dots is unreadable raw."""
    frame = frame.astype(np.float32)
    lo, hi = np.percentile(frame, [low_pct, high_pct])
    if hi <= lo:
        lo, hi = float(frame.min()), float(frame.max() or 1.0)
    return np.clip((frame - lo) * (255.0 / max(1e-6, hi - lo)), 0, 255).astype(np.uint8)


def render(frame: np.ndarray,
           projection: Optional[np.ndarray] = None,
           sample: Optional[Dict[str, Any]] = None,
           *, overlay: bool = True, stretch: bool = True,
           quality: int = 80, scale: float = 1.0,
           max_width: Optional[int] = None) -> bytes:
    """Render one annotated JPEG. Returns the encoded bytes."""
    if not _HAVE_PIL:
        raise RuntimeError("Pillow is required for the debug image endpoints")

    data = _stretch(frame) if stretch else np.clip(frame, 0, 255).astype(np.uint8)
    image = Image.fromarray(data).convert("RGB")

    # Cap the width before annotating, so the labels stay legible instead of
    # being shrunk with the image. A full-frame preview is never wanted: it is
    # for looking at, and encoding one costs more than the estimator it would
    # be stealing time from.
    effective = float(scale or 1.0)
    if max_width and image.width * effective > max_width:
        effective = max_width / image.width
    if effective != 1.0:
        image = image.resize((max(1, int(image.width * effective)),
                              max(1, int(image.height * effective))),
                             Image.BILINEAR if effective < 1.0 else Image.NEAREST)

    if overlay:
        _annotate(image, projection, sample or {})

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=int(quality))
    return buffer.getvalue()


def _annotate(image: "Image.Image", projection: Optional[np.ndarray],
              sample: Dict[str, Any]) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    scale_x = width / len(projection) if projection is not None and len(projection) else 1.0

    # Projection trace across the bottom third, so it lines up with the image
    # columns above it and you can see peak position and shape at a glance.
    if projection is not None and len(projection) > 1:
        band = height // 3
        peak = float(np.max(projection)) or 1.0
        points = [(x * scale_x, height - 1 - (float(v) / peak) * (band - 2))
                  for x, v in enumerate(projection)]
        draw.line(points, fill=(80, 200, 255), width=1)
        draw.line([(0, height - band), (width, height - band)], fill=(60, 60, 60), width=1)

    for key, colour, label in (("left_peak_x", (255, 90, 90), "L"),
                               ("right_peak_x", (120, 255, 120), "R")):
        value = sample.get(key)
        if value is None:
            continue
        x = float(value) * scale_x
        draw.line([(x, 0), (x, height)], fill=colour, width=1)
        draw.text((x + 3, 2), f"{label} {float(value):.2f}", fill=colour)

    lines = []
    if sample.get("focus") is not None:
        lines.append(f"focus {sample['focus']:.2f} px")
    if sample.get("x_peak_distance") is not None:
        lines.append(f"sep {sample['x_peak_distance']:.2f} px")
    if sample.get("quality") is not None:
        lines.append(f"q {sample['quality']:.1f}")
    if sample.get("z_um") is not None:
        lines.append(f"z {sample['z_um']:.3f} um")
    if not sample.get("valid", True):
        lines.append("INVALID")

    y = 2
    for line in lines:
        draw.text((4, y), line, fill=(255, 255, 0))
        y += 12
