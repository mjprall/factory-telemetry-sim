"""Unit tests for the pure simulation/detection logic (plain CPython, no browser)."""

import math
import random
import statistics

import pytest

from sim import (DetectorConfig, Debouncer, FaultKind, Message, Network, Plant,
                 ReorderBuffer, RollingStats, Severity, SignalDetector, SignalSpec,
                 Status, classify, format_clock)


# ---------------------------------------------------------------- RollingStats

def test_rolling_stats_matches_statistics_module():
    rng = random.Random(1)
    data = [rng.gauss(12_000, 40) for _ in range(500)]  # large mean: tests float stability
    rs = RollingStats(window=50)
    for i, x in enumerate(data):
        rs.push(x)
        win = data[max(0, i - 49): i + 1]
        assert rs.mean == pytest.approx(statistics.fmean(win), rel=1e-12)
        if len(win) >= 2:
            assert rs.std == pytest.approx(statistics.pstdev(win), rel=1e-6)


def test_rolling_stats_window_and_resize():
    rs = RollingStats(3)
    for x in (1, 2, 3, 4):
        rs.push(x)
    assert len(rs) == 3 and rs.mean == 3
    rs.resize(2)
    assert len(rs) == 2 and rs.mean == 3.5
    with pytest.raises(ValueError):
        RollingStats(1)


def test_rolling_stats_constant_input_has_zero_std():
    rs = RollingStats(10)
    for _ in range(10):
        rs.push(0.1)
    assert rs.std == pytest.approx(0.0, abs=1e-12)
    assert not math.isnan(rs.std)


# ---------------------------------------------------------------- Debouncer

def test_debouncer_needs_consecutive_samples_to_raise():
    d = Debouncer(on_count=3, off_count=2)
    seq = [Severity.WARN, Severity.WARN, Severity.OK, Severity.WARN, Severity.WARN]
    assert not any(d.update(s) for s in seq)  # interrupted streak never raises
    assert d.level is Severity.OK
    assert d.update(Severity.WARN) is True
    assert d.level is Severity.WARN


def test_debouncer_needs_off_count_to_clear():
    d = Debouncer(on_count=1, off_count=3)
    d.update(Severity.ALARM)
    assert d.level is Severity.ALARM
    d.update(Severity.OK); d.update(Severity.OK)
    assert d.level is Severity.ALARM
    d.update(Severity.ALARM)                      # blip resets the clear streak
    d.update(Severity.OK); d.update(Severity.OK)
    assert d.level is Severity.ALARM
    assert d.update(Severity.OK) and d.level is Severity.OK


def test_debouncer_escalation_streak_carries_from_warn_to_alarm():
    d = Debouncer(on_count=3, off_count=3)
    d.update(Severity.WARN); d.update(Severity.WARN)
    d.update(Severity.ALARM)  # 3rd consecutive elevated sample
    assert d.level is Severity.ALARM


def test_debouncer_does_not_flap_on_alternating_input():
    d = Debouncer(on_count=3, off_count=8)
    changes = sum(d.update(Severity.WARN if i % 2 else Severity.OK) for i in range(200))
    assert changes == 0


# ---------------------------------------------------------------- classify / hysteresis

SPEC = SignalSpec("temp", "C", 50, 1.0, high_limit=90)


def test_classify_thresholds_and_hard_limit():
    cfg = DetectorConfig(z_warn=3.0, alarm_factor=1.5)
    assert classify(2.9, 50, SPEC, Severity.OK, cfg) is Severity.OK
    assert classify(3.0, 50, SPEC, Severity.OK, cfg) is Severity.WARN
    assert classify(-4.6, 50, SPEC, Severity.OK, cfg) is Severity.ALARM  # both tails
    assert classify(0.0, 95, SPEC, Severity.OK, cfg) is Severity.ALARM   # hard limit


def test_classify_hysteresis_band():
    cfg = DetectorConfig(z_warn=3.0, clear_ratio=0.75)
    # 2.5 would not raise from OK, but keeps an existing WARN (bar drops to 2.25).
    assert classify(2.5, 50, SPEC, Severity.OK, cfg) is Severity.OK
    assert classify(2.5, 50, SPEC, Severity.WARN, cfg) is Severity.WARN
    assert classify(2.0, 50, SPEC, Severity.WARN, cfg) is Severity.OK


# ---------------------------------------------------------------- SignalDetector

def feed(det, values, start=0):
    return [det.update(start + i, v) for i, v in enumerate(values)]


def test_detector_quiet_on_pure_noise():
    rng = random.Random(7)
    det = SignalDetector(SPEC, DetectorConfig())
    results = feed(det, [rng.gauss(50, 1) for _ in range(5000)])
    assert sum(changed for _, changed in results) <= 2  # ~no false alerts after debounce


def test_detector_warmup_never_alerts():
    det = SignalDetector(SPEC, DetectorConfig(min_samples=20))
    results = feed(det, [50] * 5 + [500] * 10)
    assert all(s.level is Severity.OK or s.value >= 90 for s, _ in results[:5])
    assert not any(s.ready for s, _ in results[:15])


def test_detector_catches_step_and_clears_after_recovery():
    rng = random.Random(3)
    cfg = DetectorConfig(on_count=3, off_count=5)
    det = SignalDetector(SPEC, cfg)
    feed(det, [rng.gauss(50, 1) for _ in range(100)])
    feed(det, [58 + rng.gauss(0, 1) for _ in range(10)], start=100)
    assert det.level is Severity.ALARM
    feed(det, [rng.gauss(50, 1) for _ in range(20)], start=110)
    assert det.level is Severity.OK


def test_detector_does_not_learn_a_slow_ramp():
    """A naive rolling z-score absorbs a ramp (z plateaus ~1.7). Ours must not."""
    rng = random.Random(5)
    det = SignalDetector(SignalSpec("vib", "mm/s", 2.0, 0.1), DetectorConfig())
    feed(det, [rng.gauss(2.0, 0.1) for _ in range(100)])
    results = feed(det, [2.0 + 0.05 * i + rng.gauss(0, 0.1) for i in range(120)], start=100)
    assert det.level is Severity.ALARM
    first_alarm = next(i for i, (s, _) in enumerate(results) if s.level is Severity.ALARM)
    assert first_alarm < 30


def test_detector_threshold_change_takes_effect_live():
    cfg = DetectorConfig(z_warn=3.0)
    det = SignalDetector(SPEC, cfg)
    rng = random.Random(11)
    feed(det, [rng.gauss(50, 1) for _ in range(100)])
    cfg.z_warn = 50.0  # visitor drags the slider way up
    feed(det, [57] * 10, start=100)
    assert det.level is Severity.OK


# ---------------------------------------------------------------- ReorderBuffer / Network

def msg(seq):
    return Message("M", seq, seq, {"x": float(seq)})


def test_reorder_buffer_restores_order():
    buf = ReorderBuffer(max_wait=5)
    for s in (0, 2, 1, 4, 3):
        buf.push(msg(s), now=0)
    assert [m.seq for m in buf.pop_ready(0)] == [0, 1, 2, 3, 4]
    assert buf.out_of_order == 2


def test_reorder_buffer_waits_then_skips_gap():
    buf = ReorderBuffer(max_wait=3)
    buf.push(msg(0), 0)
    buf.push(msg(2), 0)
    assert [m.seq for m in buf.pop_ready(0)] == [0]      # 1 missing: hold 2
    assert buf.pop_ready(2) == []
    assert [m.seq for m in buf.pop_ready(3)] == [2]      # gave up on 1
    assert buf.skipped == 1
    buf.push(msg(1), 4)                                  # too late now
    assert buf.late == 1 and buf.pop_ready(4) == []


def test_reorder_buffer_drops_duplicates():
    buf = ReorderBuffer()
    buf.push(msg(0), 0); buf.push(msg(0), 0)
    assert len(buf.pop_ready(0)) == 1 and buf.late == 1


def test_network_jitter_reorders_but_loses_nothing():
    rng = random.Random(2)
    net = Network(rng, max_jitter=4)
    for t in range(100):
        net.send(msg(t), t, delayed=True)
    got = []
    for t in range(110):
        got.extend(m.seq for m in net.deliver(t))
    assert sorted(got) == list(range(100))
    assert got != sorted(got)


# ---------------------------------------------------------------- Plant (integration)

def test_plant_is_deterministic_with_seed():
    a, b = Plant(seed=9), Plant(seed=9)
    a.run(50); b.run(50)
    assert a.latest("CNC-01") == b.latest("CNC-01")


def test_plant_baseline_has_no_alerts():
    p = Plant(seed=123)
    events = p.run(1500)
    assert events == []
    assert all(p.status(s.id) is Status.OK for s in p.specs)


@pytest.mark.parametrize("kind, machine, signal", [
    (FaultKind.BEARING_WEAR, "CNC-01", "vibration"),
    (FaultKind.OVERHEAT, "PRS-03", "oil_temp"),
    (FaultKind.OVERHEAT, "CMP-04", "temp"),
])
def test_drift_faults_reach_alarm(kind, machine, signal):
    p = Plant(seed=4)
    p.run(100)
    p.inject(machine, kind)
    p.run(60)
    assert p.status(machine) is Status.ALARM
    assert p.detectors[machine][signal].level is Severity.ALARM
    assert all(p.status(s.id) is Status.OK for s in p.specs if s.id != machine)


def test_dropout_goes_stale_then_recovers():
    p = Plant(seed=4, stale_after=6)
    p.run(100)
    p.inject("CMP-04", FaultKind.DROPOUT)
    p.run(8)
    assert p.status("CMP-04") is Status.STALE
    p.run(20)
    assert p.status("CMP-04") is not Status.STALE
    texts = [e.message for e in p.events if e.machine_id == "CMP-04"]
    assert any("stale" in t for t in texts) and "Data resumed" in texts
    assert any(t.startswith("Fault ended") for t in texts)


def test_spike_alarms_then_clears():
    p = Plant(seed=4)
    p.run(100)
    p.inject("CNC-02", FaultKind.SPIKE)
    p.run(6)
    assert p.status("CNC-02") is Status.ALARM
    p.run(30)
    assert p.status("CNC-02") is Status.OK


def test_network_delay_is_resequenced_without_false_alarms():
    p = Plant(seed=4)
    p.run(100)
    p.inject("CNC-02", FaultKind.NETWORK_DELAY)
    p.run(60)
    s = p.stats
    assert s.out_of_order > 0
    assert s.sent == s.received
    assert p.status("CNC-02") is Status.OK
    ts = [x.ts for x in p.history["CNC-02"]["rpm"]]
    assert ts == sorted(ts), "detector must see samples in measurement order"


def test_maintenance_clears_faults_and_alarm():
    p = Plant(seed=4)
    p.run(100)
    p.inject("CNC-01", FaultKind.BEARING_WEAR)
    p.run(60)
    p.clear_faults("CNC-01")
    assert p.faults("CNC-01") == []
    assert p.status("CNC-01") is Status.OK
    p.run(100)
    assert p.status("CNC-01") is Status.OK


def test_reset_restores_clean_state():
    p = Plant(seed=4)
    p.inject("CNC-01", FaultKind.OVERHEAT)
    p.run(80)
    p.reset()
    assert p.now == 0 and p.events == [] and p.faults("CNC-01") == []


def test_format_clock():
    assert format_clock(0) == "06:00:00"
    assert format_clock(3661) == "07:01:01"
    assert format_clock(18 * 3600) == "00:00:00"  # wraps at midnight


def test_fault_expiry_is_logged_even_if_ui_queries_faults_every_tick():
    p = Plant(seed=4)
    p.inject("CNC-02", FaultKind.SPIKE)
    for _ in range(10):
        p.tick()
        p.faults("CNC-02")  # the UI polls this between ticks; must not swallow expiry
    assert any(e.message == "Fault ended: spike" for e in p.events)
    assert p.faults("CNC-02") == []


def test_readings_respect_physical_floor_even_during_spikes():
    p = Plant(seed=1)
    for s in p.specs:
        p.inject(s.id, FaultKind.SPIKE)
    p.run(6)
    assert all(x.value >= 0 for m in p.history.values() for h in m.values() for x in h)
