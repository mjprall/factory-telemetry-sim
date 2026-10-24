"""Chart geometry: turn samples into SVG path strings.

Pure Python (no DOM). The browser glue hands these strings to ``Path2D`` so a
whole series is drawn with a single JS call instead of hundreds of ``lineTo``s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from sim import Sample, Severity


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def right(self) -> float:
        return self.x + self.w


def value_range(samples: Sequence[Sample], z: float, pad: float = 0.08,
                min_span: float = 1e-6) -> tuple[float, float]:
    """Y-axis range covering values and the ±z band, with padding."""
    if not samples:
        return 0.0, 1.0
    lo = min(s.value for s in samples)
    hi = max(s.value for s in samples)
    for s in samples:
        if s.ready:  # warm-up samples have no meaningful baseline yet
            lo, hi = min(lo, s.mean - z * s.std), max(hi, s.mean + z * s.std)
    span = max(hi - lo, min_span, abs(hi) * 1e-3)
    mid = (lo + hi) / 2
    lo, hi = mid - span / 2, mid + span / 2
    return lo - span * pad, hi + span * pad


def scale(v: float, lo: float, hi: float, a: float, b: float) -> float:
    """Linear map v in [lo, hi] to [a, b]."""
    if hi == lo:
        return (a + b) / 2
    return a + (v - lo) * (b - a) / (hi - lo)


class Projector:
    """Maps (tick, value) into a rectangle (screen y grows downward)."""

    def __init__(self, rect: Rect, t0: float, t1: float, lo: float, hi: float) -> None:
        self.rect, self.t0, self.t1, self.lo, self.hi = rect, t0, t1, lo, hi

    def x(self, t: float) -> float:
        return scale(t, self.t0, self.t1, self.rect.x, self.rect.right)

    def y(self, v: float) -> float:
        return scale(v, self.lo, self.hi, self.rect.bottom, self.rect.y)


def _fmt(n: float) -> str:
    return f"{n:.1f}"


def split_on_gaps(samples: Sequence[Sample], max_gap: int = 1) -> list[list[Sample]]:
    """Split into contiguous runs so dropouts show as breaks, not straight lines."""
    runs: list[list[Sample]] = []
    for s in samples:
        if runs and s.ts - runs[-1][-1].ts <= max_gap:
            runs[-1].append(s)
        else:
            runs.append([s])
    return runs


def line_path(points: Iterable[tuple[float, float]]) -> str:
    parts = []
    for i, (x, y) in enumerate(points):
        parts.append(f"{'M' if i == 0 else 'L'}{_fmt(x)} {_fmt(y)}")
    return "".join(parts)


def series_path(samples: Sequence[Sample], p: Projector) -> str:
    return "".join(line_path((p.x(s.ts), p.y(s.value)) for s in run)
                   for run in split_on_gaps(samples))


def band_path(samples: Sequence[Sample], p: Projector, z: float) -> str:
    """Closed polygon for mean ± z·std (the 'expected' envelope)."""
    out = []
    for run in split_on_gaps([s for s in samples if s.ready]):
        if len(run) < 2:
            continue
        upper = [(p.x(s.ts), p.y(s.mean + z * s.std)) for s in run]
        lower = [(p.x(s.ts), p.y(s.mean - z * s.std)) for s in reversed(run)]
        out.append(line_path(upper + lower) + "Z")
    return "".join(out)


def alert_points(samples: Sequence[Sample], p: Projector) -> list[tuple[float, float, Severity]]:
    return [(p.x(s.ts), p.y(s.value), s.level) for s in samples if s.level > Severity.OK]


def format_value(v: float) -> str:
    """Compact human formatting: 12 034, 58.3, 2.21."""
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}".replace(",", "\u2009")
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def dots_path(points: Iterable[tuple[float, float]], r: float = 2.5) -> str:
    """Many small circles as one SVG path (two arcs per circle)."""
    d = 2 * r
    return "".join(
        f"M{_fmt(x - r)} {_fmt(y)}a{r} {r} 0 1 0 {d} 0a{r} {r} 0 1 0 {-d} 0"
        for x, y in points
    )
