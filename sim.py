"""Factory telemetry simulation and anomaly detection.

Pure Python, standard library only, no browser imports. Everything here runs
unchanged under CPython (for the tests) and under Pyodide (in the browser).

Pipeline, once per tick (one simulated second)::

    Machine.emit()  ->  Network.send()/deliver()  ->  ReorderBuffer  ->  Detector
       (sensors)         (optional delay/jitter)       (restore order)    (z-score +
                                                                           hysteresis)
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Iterable, Iterator

# --------------------------------------------------------------------------- #
# Domain types
# --------------------------------------------------------------------------- #


class Severity(IntEnum):
    """Ordered so max() of a set of severities gives the worst one."""

    OK = 0
    WARN = 1
    ALARM = 2


class Status(str, Enum):
    """What a machine tile shows. STALE overrides severity: no data, no verdict.
    INFO is only used for operator/log events (fault injected, maintenance)."""

    INFO = "INFO"
    OK = "OK"
    WARN = "WARN"
    ALARM = "ALARM"
    STALE = "STALE"


class FaultKind(str, Enum):
    BEARING_WEAR = "bearing_wear"    # vibration ramps up (and a little heat)
    OVERHEAT = "overheat"            # temperature drifts upward
    DROPOUT = "dropout"              # sensor gateway stops sending
    SPIKE = "spike"                  # short burst of large noise on every signal
    NETWORK_DELAY = "network_delay"  # random per-message latency -> out-of-order


@dataclass(frozen=True)
class SignalSpec:
    name: str
    unit: str
    nominal: float
    noise: float              # std-dev of white measurement noise
    drift: float = 0.0        # std-dev of the slow random-walk (process) component
    cycle_amp: float = 0.0    # amplitude of a periodic duty-cycle component
    cycle_period: float = 30  # ticks
    high_limit: float | None = None  # hard engineering limit (always ALARM)
    min_value: float = 0.0    # physical floor (no negative RPM or vibration)


@dataclass(frozen=True)
class MachineSpec:
    id: str
    name: str
    kind: str
    signals: tuple[SignalSpec, ...]


@dataclass(frozen=True)
class Message:
    """One telemetry frame from a machine: all of its signals at one instant."""

    machine_id: str
    seq: int
    ts: int                     # tick at which it was measured
    values: dict[str, float]


@dataclass(frozen=True)
class Event:
    ts: int
    machine_id: str
    signal: str | None
    severity: Status
    message: str


@dataclass
class Fault:
    kind: FaultKind
    start: int
    duration: int | None = None  # None = until cleared (wear does not self-heal)

    def active(self, now: int) -> bool:
        return self.duration is None or now < self.start + self.duration

    def age(self, now: int) -> int:
        return now - self.start


DEFAULT_FAULT_DURATION: dict[FaultKind, int | None] = {
    FaultKind.BEARING_WEAR: None,
    FaultKind.OVERHEAT: None,
    FaultKind.DROPOUT: 15,
    FaultKind.SPIKE: 6,
    FaultKind.NETWORK_DELAY: 40,
}


def _spindle(id_: str, name: str, rpm: float) -> MachineSpec:
    return MachineSpec(
        id=id_, name=name, kind="CNC spindle",
        signals=(
            SignalSpec("temp", "°C", 58.0, 0.25, drift=0.03, high_limit=85.0),
            SignalSpec("vibration", "mm/s", 2.2, 0.12, drift=0.01, high_limit=7.1),
            SignalSpec("rpm", "rpm", rpm, 25.0, drift=2.0, cycle_amp=60, cycle_period=45),
        ),
    )


LINE_1: tuple[MachineSpec, ...] = (
    _spindle("CNC-01", "5-axis mill", 12_000),
    _spindle("CNC-02", "Lathe", 3_500),
    MachineSpec(
        id="PRS-03", name="Hydraulic press", kind="Press",
        signals=(
            SignalSpec("pressure", "bar", 180.0, 1.2, drift=0.1,
                       cycle_amp=6.0, cycle_period=12, high_limit=215.0),
            SignalSpec("oil_temp", "°C", 46.0, 0.2, drift=0.03, high_limit=70.0),
            SignalSpec("vibration", "mm/s", 1.4, 0.08, drift=0.01, high_limit=6.0),
        ),
    ),
    MachineSpec(
        id="CMP-04", name="Air compressor", kind="Compressor",
        signals=(
            SignalSpec("air_pressure", "bar", 7.0, 0.04, drift=0.004,
                       cycle_amp=0.12, cycle_period=60),
            SignalSpec("current", "A", 42.0, 0.5, drift=0.05, high_limit=60.0),
            SignalSpec("temp", "°C", 71.0, 0.3, drift=0.03, high_limit=95.0),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


class Machine:
    """Generates readings for one machine: nominal + duty cycle + drift + noise + faults."""

    def __init__(self, spec: MachineSpec, rng: random.Random) -> None:
        self.spec = spec
        self.rng = rng
        self.faults: list[Fault] = []
        self._seq = 0
        self._walk = {s.name: 0.0 for s in spec.signals}
        self._phase = rng.uniform(0, 2 * math.pi)

    def inject(self, kind: FaultKind, now: int, duration: int | None = ...) -> Fault:  # type: ignore[assignment]
        if duration is ...:
            duration = DEFAULT_FAULT_DURATION[kind]
        # Re-injecting the same fault restarts it rather than stacking it.
        self.faults = [f for f in self.faults if f.kind is not kind]
        fault = Fault(kind, now, duration)
        self.faults.append(fault)
        return fault

    def clear_faults(self) -> None:
        self.faults.clear()

    def active_faults(self, now: int) -> list[Fault]:
        return [f for f in self.faults if f.active(now)]

    def expire_faults(self, now: int) -> list[Fault]:
        """Remove faults whose duration has elapsed and return them."""
        expired = [f for f in self.faults if not f.active(now)]
        self.faults = [f for f in self.faults if f.active(now)]
        return expired

    def has_fault(self, kind: FaultKind, now: int) -> bool:
        return any(f.kind is kind for f in self.active_faults(now))

    def _fault_offset(self, sig: SignalSpec, now: int) -> float:
        offset = 0.0
        for f in self.active_faults(now):
            age = f.age(now)
            if f.kind is FaultKind.BEARING_WEAR:
                if "vibration" in sig.name:
                    offset += 0.05 * age + 0.0004 * age * age  # accelerating wear
                elif "temp" in sig.name:
                    offset += 0.02 * age
            elif f.kind is FaultKind.OVERHEAT and "temp" in sig.name:
                offset += 0.15 * age
            elif f.kind is FaultKind.SPIKE:
                offset += self.rng.gauss(0, sig.noise * 12)
        return offset

    def emit(self, now: int) -> Message | None:
        """Sample all sensors. Returns None while the gateway is dropped out."""
        if self.has_fault(FaultKind.DROPOUT, now):
            return None
        values = {}
        for sig in self.spec.signals:
            # Mean-reverting random walk keeps drift bounded (Ornstein-Uhlenbeck-ish).
            walk = self._walk[sig.name] * 0.98 + self.rng.gauss(0, sig.drift)
            self._walk[sig.name] = walk
            cycle = sig.cycle_amp * math.sin(2 * math.pi * now / sig.cycle_period + self._phase)
            value = (sig.nominal + cycle + walk + self.rng.gauss(0, sig.noise)
                     + self._fault_offset(sig, now))
            values[sig.name] = round(max(value, sig.min_value), 3)
        msg = Message(self.spec.id, self._seq, now, values)
        self._seq += 1
        return msg


class Network:
    """In-flight messages. Normally zero latency; a delay fault adds random jitter,
    which is what produces out-of-order delivery."""

    def __init__(self, rng: random.Random, max_jitter: int = 4) -> None:
        self.rng = rng
        self.max_jitter = max_jitter
        self._in_flight: list[tuple[int, int, Message]] = []  # (deliver_at, order, msg)
        self._order = 0

    def send(self, msg: Message, now: int, delayed: bool) -> None:
        delay = self.rng.randint(0, self.max_jitter) if delayed else 0
        self._in_flight.append((now + delay, self._order, msg))
        self._order += 1

    def deliver(self, now: int) -> list[Message]:
        due = [item for item in self._in_flight if item[0] <= now]
        self._in_flight = [item for item in self._in_flight if item[0] > now]
        # Within a tick, deliver in the jittered order they "arrive".
        self.rng.shuffle(due)
        return [msg for _, _, msg in due]

    def clear(self) -> None:
        self._in_flight.clear()


class ReorderBuffer:
    """Restores per-source sequence order.

    Messages are released strictly in seq order. If a gap is not filled within
    ``max_wait`` ticks we give up on the missing seq(s) and move on, so one lost
    packet cannot stall the stream forever. Messages that arrive after their slot
    was skipped are counted as ``late`` and dropped.
    """

    def __init__(self, max_wait: int = 5) -> None:
        self.max_wait = max_wait
        self.next_seq = 0
        self._pending: dict[int, tuple[Message, int]] = {}
        self._max_seen = -1
        self.out_of_order = 0
        self.late = 0
        self.skipped = 0

    def push(self, msg: Message, now: int) -> None:
        if msg.seq < self.next_seq or msg.seq in self._pending:
            self.late += 1
            return
        if msg.seq < self._max_seen:
            self.out_of_order += 1
        self._max_seen = max(self._max_seen, msg.seq)
        self._pending[msg.seq] = (msg, now)

    def pop_ready(self, now: int) -> list[Message]:
        ready: list[Message] = []
        while self._pending:
            if self.next_seq in self._pending:
                ready.append(self._pending.pop(self.next_seq)[0])
                self.next_seq += 1
                continue
            oldest_arrival = min(arrived for _, arrived in self._pending.values())
            if now - oldest_arrival < self.max_wait:
                break
            # Gap timed out: skip to the lowest seq we actually have.
            lowest = min(self._pending)
            self.skipped += lowest - self.next_seq
            self.next_seq = lowest
        return ready

    @property
    def depth(self) -> int:
        return len(self._pending)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


class RollingStats:
    """Fixed-window mean / population std-dev in O(1) per update.

    Uses Welford's update (and its inverse for the sample leaving the window)
    instead of sum / sum-of-squares, which suffers catastrophic cancellation
    when the mean is large relative to the spread (e.g. 12 000 rpm ± 25).
    """

    def __init__(self, window: int) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = window
        self._values: deque[float] = deque()
        self._mean = 0.0
        self._m2 = 0.0  # sum of squared deviations from the mean

    def __len__(self) -> int:
        return len(self._values)

    def _add(self, x: float) -> None:
        self._values.append(x)
        delta = x - self._mean
        self._mean += delta / len(self._values)
        self._m2 += delta * (x - self._mean)

    def _remove_oldest(self) -> None:
        x = self._values.popleft()
        n = len(self._values)
        if n == 0:
            self._mean = self._m2 = 0.0
            return
        delta = x - self._mean
        self._mean -= delta / n
        self._m2 -= delta * (x - self._mean)

    def push(self, x: float) -> None:
        self._add(x)
        if len(self._values) > self.window:
            self._remove_oldest()

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        n = len(self._values)
        if n < 2:
            return 0.0
        return math.sqrt(max(self._m2 / n, 0.0))  # guard tiny negative float error

    def resize(self, window: int) -> None:
        self.window = window
        while len(self._values) > window:
            self._remove_oldest()


@dataclass
class DetectorConfig:
    window: int = 60            # samples in the rolling baseline
    z_warn: float = 3.0         # |z| at or above -> WARN
    alarm_factor: float = 1.6   # ALARM at z_warn * alarm_factor
    clear_ratio: float = 0.75   # hysteresis: must drop below z * clear_ratio to clear
    on_count: int = 3           # consecutive samples needed to raise
    off_count: int = 8          # consecutive samples needed to clear
    min_samples: int = 20       # warm-up before scoring
    std_floor_ratio: float = 0.5  # std never below noise * ratio (avoid /0)

    @property
    def z_alarm(self) -> float:
        return self.z_warn * self.alarm_factor


class Debouncer:
    """Turns a noisy per-sample severity into a stable alert level.

    Escalation needs ``on_count`` consecutive samples at or above the new level;
    de-escalation needs ``off_count`` consecutive samples below the current level.
    """

    def __init__(self, on_count: int, off_count: int) -> None:
        self.on_count = on_count
        self.off_count = off_count
        self.level = Severity.OK
        self._candidate = Severity.OK
        self._streak = 0

    def update(self, raw: Severity) -> bool:
        """Feed one raw sample; return True if the stable level changed."""
        if raw == self.level:
            self._streak = 0
            return False
        if raw != self._candidate:
            # A different direction/level resets the streak, except when we are
            # escalating further (WARN streak continues into ALARM streak).
            escalating_further = raw > self.level and self._candidate > self.level
            self._candidate = raw
            self._streak = self._streak if escalating_further else 0
        self._streak += 1
        needed = self.on_count if raw > self.level else self.off_count
        if self._streak >= needed:
            self.level = raw
            self._streak = 0
            return True
        return False


def classify(z: float, value: float, spec: SignalSpec, current: Severity,
             cfg: DetectorConfig) -> Severity:
    """Raw severity for one sample, with hysteresis relative to the current level."""
    if spec.high_limit is not None and value >= spec.high_limit:
        return Severity.ALARM
    a = abs(z)
    # Hysteresis: once at a level, stay there until |z| falls below a lower bar.
    alarm_bar = cfg.z_alarm * (cfg.clear_ratio if current >= Severity.ALARM else 1.0)
    warn_bar = cfg.z_warn * (cfg.clear_ratio if current >= Severity.WARN else 1.0)
    if a >= alarm_bar:
        return Severity.ALARM
    if a >= warn_bar:
        return Severity.WARN
    return Severity.OK


@dataclass
class Sample:
    """What the charts need for one point."""

    ts: int
    value: float
    mean: float
    std: float
    z: float
    level: Severity
    ready: bool = True  # False during warm-up (mean/std not meaningful yet)


class SignalDetector:
    """Rolling z-score + debounce for a single signal."""

    def __init__(self, spec: SignalSpec, cfg: DetectorConfig) -> None:
        self.spec = spec
        self.cfg = cfg
        self.stats = RollingStats(cfg.window)
        self.debounce = Debouncer(cfg.on_count, cfg.off_count)

    @property
    def level(self) -> Severity:
        return self.debounce.level

    def update(self, ts: int, value: float) -> tuple[Sample, bool]:
        cfg = self.cfg
        if self.stats.window != cfg.window:
            self.stats.resize(cfg.window)
        self.debounce.on_count, self.debounce.off_count = cfg.on_count, cfg.off_count

        mean = self.stats.mean
        std = max(self.stats.std, self.spec.noise * cfg.std_floor_ratio)
        warmed_up = len(self.stats) >= cfg.min_samples
        z = (value - mean) / std if warmed_up else 0.0
        raw = classify(z, value, self.spec, self.level, cfg) if warmed_up else Severity.OK
        changed = self.debounce.update(raw)

        # Score against the baseline *before* adding the point, and only learn
        # from normal-looking samples: a drifting fault must not become the new
        # "normal" (a plain rolling z-score happily absorbs a slow ramp).
        if raw == Severity.OK:
            self.stats.push(value)
        return Sample(ts, value, mean, std, z, self.level, warmed_up), changed

    def reset(self) -> None:
        self.stats = RollingStats(self.cfg.window)
        self.debounce = Debouncer(self.cfg.on_count, self.cfg.off_count)


# --------------------------------------------------------------------------- #
# The whole line
# --------------------------------------------------------------------------- #


@dataclass
class PlantStats:
    ticks: int = 0
    sent: int = 0
    received: int = 0
    out_of_order: int = 0
    late: int = 0
    skipped: int = 0


def format_clock(tick: int, shift_start: int = 6 * 3600) -> str:
    """Tick (seconds since shift start) -> HH:MM:SS wall-clock string."""
    t = (shift_start + tick) % 86_400
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


class Plant:
    """Owns the machines, the network, per-machine reorder buffers and detectors."""

    def __init__(self, specs: Iterable[MachineSpec] = LINE_1, *, seed: int | None = 42,
                 cfg: DetectorConfig | None = None, history: int = 240,
                 stale_after: int = 6, reorder_wait: int = 5) -> None:
        self.specs = tuple(specs)
        self.seed = seed
        self.cfg = cfg or DetectorConfig()
        self.history_len = history
        self.stale_after = stale_after
        self.reorder_wait = reorder_wait
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.now = 0
        self.machines = {s.id: Machine(s, self.rng) for s in self.specs}
        self.network = Network(self.rng)
        self.buffers = {s.id: ReorderBuffer(self.reorder_wait) for s in self.specs}
        self.detectors = {
            s.id: {sig.name: SignalDetector(sig, self.cfg) for sig in s.signals}
            for s in self.specs
        }
        self.history: dict[str, dict[str, deque[Sample]]] = {
            s.id: {sig.name: deque(maxlen=self.history_len) for sig in s.signals}
            for s in self.specs
        }
        self.last_seen = {s.id: 0 for s in self.specs}
        self.stale = {s.id: False for s in self.specs}
        self.events: list[Event] = []
        self._stats = PlantStats()

    # -- controls ------------------------------------------------------------

    def inject(self, machine_id: str, kind: FaultKind) -> Event:
        fault = self.machines[machine_id].inject(kind, self.now)
        dur = "until cleared" if fault.duration is None else f"for {fault.duration}s"
        return self._log(machine_id, None, Status.INFO,
                         f"Fault injected: {kind.value.replace('_', ' ')} ({dur})")

    def clear_faults(self, machine_id: str) -> Event:
        """Simulate a maintenance visit: remove faults and re-learn baselines
        (after a bearing swap the old "normal" no longer applies)."""
        self.machines[machine_id].clear_faults()
        for det in self.detectors[machine_id].values():
            det.reset()
        return self._log(machine_id, None, Status.INFO,
                         "Maintenance: faults cleared, baselines re-learning")

    # -- main loop -----------------------------------------------------------

    def tick(self) -> list[Event]:
        """Advance one simulated second; return the events raised this tick."""
        start = len(self.events)
        now = self.now

        for mid, machine in self.machines.items():
            for fault in machine.expire_faults(now):
                self._log(mid, None, Status.INFO,
                          f"Fault ended: {fault.kind.value.replace('_', ' ')}")
            msg = machine.emit(now)
            if msg is not None:
                self._stats.sent += 1
                self.network.send(msg, now, machine.has_fault(FaultKind.NETWORK_DELAY, now))

        for msg in self.network.deliver(now):
            self._stats.received += 1
            self.last_seen[msg.machine_id] = now
            self.buffers[msg.machine_id].push(msg, now)

        for mid, buf in self.buffers.items():
            for msg in buf.pop_ready(now):
                self._process(msg)
            self._check_stale(mid, now)

        self.now += 1
        self._stats.ticks += 1
        return self.events[start:]

    def run(self, ticks: int) -> list[Event]:
        out: list[Event] = []
        for _ in range(ticks):
            out.extend(self.tick())
        return out

    def _process(self, msg: Message) -> None:
        for name, value in msg.values.items():
            det = self.detectors[msg.machine_id][name]
            before = det.level
            sample, changed = det.update(msg.ts, value)
            self.history[msg.machine_id][name].append(sample)
            if changed:
                self._log_transition(msg.machine_id, det, before, sample)

    def _log_transition(self, mid: str, det: SignalDetector, before: Severity,
                        s: Sample) -> None:
        spec = det.spec
        shown = f"{s.value:.0f}" if abs(s.value) >= 1000 else f"{s.value:.2f}"
        detail = f"{spec.name}={shown} {spec.unit} (z={s.z:+.1f})"
        if spec.high_limit is not None and s.value >= spec.high_limit:
            detail += f" ≥ limit {spec.high_limit:g} {spec.unit}"
        if s.level > before:
            text = f"{s.level.name} raised: {detail}"
        else:
            text = f"{before.name} cleared → {s.level.name}: {detail}"
        self._log(mid, spec.name, Status(s.level.name), text, ts=s.ts)

    def _check_stale(self, mid: str, now: int) -> None:
        age = now - self.last_seen[mid]
        if not self.stale[mid] and age >= self.stale_after:
            self.stale[mid] = True
            self._log(mid, None, Status.STALE, f"No data for {age}s — readings stale")
        elif self.stale[mid] and age == 0:
            self.stale[mid] = False
            self._log(mid, None, Status.OK, "Data resumed")

    def _log(self, mid: str, signal: str | None, sev: Status, text: str,
             ts: int | None = None) -> Event:
        ev = Event(self.now if ts is None else ts, mid, signal, sev, text)
        self.events.append(ev)
        if len(self.events) > 500:
            del self.events[:100]
        return ev

    # -- read model for the UI -----------------------------------------------

    def status(self, machine_id: str) -> Status:
        if self.stale[machine_id]:
            return Status.STALE
        worst = max((d.level for d in self.detectors[machine_id].values()),
                    default=Severity.OK)
        return Status(worst.name)

    def latest(self, machine_id: str) -> dict[str, Sample | None]:
        return {name: (h[-1] if h else None)
                for name, h in self.history[machine_id].items()}

    @property
    def stats(self) -> PlantStats:
        s = self._stats
        bufs = self.buffers.values()
        s.out_of_order = sum(b.out_of_order for b in bufs)
        s.late = sum(b.late for b in bufs)
        s.skipped = sum(b.skipped for b in bufs)
        return s

    def active_alerts(self) -> int:
        return sum(d.level > Severity.OK
                   for dets in self.detectors.values() for d in dets.values())

    def faults(self, machine_id: str) -> list[FaultKind]:
        return [f.kind for f in self.machines[machine_id].active_faults(self.now)]


def iter_signals(plant: Plant) -> Iterator[tuple[MachineSpec, SignalSpec]]:
    for spec in plant.specs:
        for sig in spec.signals:
            yield spec, sig
