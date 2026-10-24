import re

from charts import (Projector, Rect, band_path, dots_path, format_value, scale,
                    series_path, split_on_gaps, value_range)
from sim import Sample, Severity


def s(ts, v, mean=10.0, std=1.0, level=Severity.OK, ready=True):
    return Sample(ts, v, mean, std, 0.0, level, ready)


def test_scale_and_projector_flip_y():
    assert scale(5, 0, 10, 0, 100) == 50
    assert scale(1, 1, 1, 0, 10) == 5  # degenerate range -> centre
    p = Projector(Rect(0, 0, 100, 50), 0, 10, 0, 1)
    assert p.x(10) == 100 and p.y(0) == 50 and p.y(1) == 0


def test_value_range_covers_values_and_band_but_ignores_warmup():
    # Warm-up sample: its value counts, its (meaningless) baseline must not.
    samples = [s(0, 10.5, mean=-100, std=0, ready=False), s(1, 10.0), s(2, 11.0)]
    lo, hi = value_range(samples, z=3)
    assert lo < 7 and hi > 13
    assert lo > 0
    assert value_range([], 3) == (0.0, 1.0)


def test_split_on_gaps():
    runs = split_on_gaps([s(t, 1) for t in (0, 1, 2, 10, 11)])
    assert [[x.ts for x in r] for r in runs] == [[0, 1, 2], [10, 11]]


def test_series_path_breaks_on_gap():
    p = Projector(Rect(0, 0, 100, 100), 0, 20, 0, 20)
    d = series_path([s(t, t) for t in (0, 1, 2, 10, 11)], p)
    assert d.count("M") == 2 and d.startswith("M0.0 100.0")


def test_band_path_is_closed_polygon():
    p = Projector(Rect(0, 0, 100, 100), 0, 10, 0, 20)
    d = band_path([s(t, 10) for t in range(5)], p, z=2)
    assert d.endswith("Z") and d.count("L") == 9


def test_dots_path_two_arcs_per_point():
    d = dots_path([(1, 1), (5, 5)], r=2)
    assert len(re.findall("a", d)) == 4
    assert dots_path([]) == ""


def test_format_value():
    assert format_value(12034.4) == "12\u2009034"
    assert format_value(180.4) == "180"
    assert format_value(58.26) == "58.3"
    assert format_value(2.214) == "2.21"
