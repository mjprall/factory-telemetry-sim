"""Browser glue: wires sim.Plant to the DOM and draws the charts.

All simulation and detection logic lives in sim.py / charts.py (pure Python,
unit-tested under CPython). This file only reads the DOM, handles events, and
paints canvases, so it is the only module that imports ``pyscript``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pyodide.ffi import create_once_callable, create_proxy
from pyodide.http import pyfetch
from pyscript import document, window

from charts import (Projector, Rect, alert_points, band_path, dots_path,
                    format_value, series_path, value_range)
from sim import Event, FaultKind, Plant, Severity, Status, format_clock

BASE_TICKS_PER_SEC = 2     # 1× speed = 2 simulated seconds per real second
FRAME_SECONDS = 0.1        # UI refresh period
MAX_LOG_ITEMS = 150

SIGNAL_COLORS = ("#38bdf8", "#a78bfa", "#34d399")
LEVEL_COLORS = {Severity.WARN: "#f5a524", Severity.ALARM: "#ef4444"}
GRID = "rgba(131,150,168,0.14)"
TEXT_MUTED = "#8396a8"


def el(id_: str):
    return document.getElementById(id_)


def make(tag: str, cls: str = "", text: str = "", **attrs):
    node = document.createElement(tag)
    if cls:
        node.className = cls
    if text:
        node.textContent = text
    for k, v in attrs.items():
        node.setAttribute(k.replace("_", "-"), str(v))
    return node


_proxies = []  # keep JS callbacks alive for the page lifetime


def on(node, event: str, handler) -> None:
    proxy = create_proxy(handler)
    _proxies.append(proxy)
    node.addEventListener(event, proxy)


@dataclass
class MachineView:
    card: object
    pill: object
    faults: object
    canvas: object
    readouts: dict = field(default_factory=dict)  # signal -> (box, value_el)
    last_status: str = ""


@dataclass
class AppState:
    plant: Plant
    running: bool = True
    speed: float = 2.0
    target: str = ""
    pending_events: list = field(default_factory=list)
    views: dict = field(default_factory=dict)


state = AppState(plant=Plant(seed=None))
state.target = state.plant.specs[0].id


# --------------------------------------------------------------------------- #
# Build the machine cards
# --------------------------------------------------------------------------- #


def build_machines() -> None:
    container = el("machines")
    target = el("target")
    for spec in state.plant.specs:
        card = make("article", "machine", data_id=spec.id, tabindex="0",
                    aria_label=f"{spec.id} {spec.name}")
        head = make("div", "machine-head")
        title = make("div")
        title.append(make("div", "machine-id", spec.id))
        title.append(make("div", "machine-name", f"{spec.name} · {spec.kind}"))
        faults = make("div", "machine-faults")
        title.append(faults)
        pill = make("span", "pill pill-ok", "OK")
        head.append(title)
        head.append(pill)
        card.append(head)

        readouts = make("div", "readouts")
        view = MachineView(card, pill, faults, None)
        for sig in spec.signals:
            box = make("div", "readout", data_level="0")
            box.append(make("div", "k", sig.name.replace("_", " ")))
            v = make("div", "v", "—")
            box.append(v)
            readouts.append(box)
            view.readouts[sig.name] = (box, v, sig.unit)
        card.append(readouts)

        canvas = make("canvas", aria_label=f"{spec.id} live charts", role="img")
        card.append(canvas)
        view.canvas = canvas
        container.append(card)
        state.views[spec.id] = view

        opt = make("option", text=f"{spec.id} — {spec.name}", value=spec.id)
        target.append(opt)

        def select(_evt, mid=spec.id):
            set_target(mid)
        on(card, "click", select)
        on(card, "keydown", lambda e, mid=spec.id: set_target(mid) if e.key == "Enter" else None)
    set_target(state.target)


def set_target(mid: str) -> None:
    state.target = mid
    el("target").value = mid
    for vid, view in state.views.items():
        view.card.classList.toggle("selected", vid == mid)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def fit_canvas(canvas):
    """Size the backing store to CSS pixels × devicePixelRatio for crisp lines."""
    dpr = window.devicePixelRatio or 1
    w, h = canvas.clientWidth, canvas.clientHeight
    bw, bh = int(w * dpr), int(h * dpr)
    if canvas.width != bw or canvas.height != bh:
        canvas.width, canvas.height = bw, bh
    ctx = canvas.getContext("2d")
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    return ctx, w, h


def draw_machine(mid: str, view: MachineView) -> None:
    plant = state.plant
    ctx, w, h = fit_canvas(view.canvas)
    if w == 0 or h == 0:
        return
    ctx.clearRect(0, 0, w, h)
    spec = next(s for s in plant.specs if s.id == mid)
    z = plant.cfg.z_warn
    rows = len(spec.signals)
    gap = 6
    row_h = (h - gap * (rows - 1)) / rows
    t1 = plant.now
    t0 = t1 - plant.history_len
    ctx.font = "10px ui-monospace, Menlo, Consolas, monospace"

    for i, sig in enumerate(spec.signals):
        rect = Rect(0, i * (row_h + gap), w, row_h)
        samples = list(plant.history[mid][sig.name])
        color = SIGNAL_COLORS[i % len(SIGNAL_COLORS)]

        ctx.fillStyle = "rgba(0,0,0,0.22)"
        ctx.fillRect(rect.x, rect.y, rect.w, rect.h)
        ctx.strokeStyle = GRID
        ctx.lineWidth = 1
        for frac in (0.25, 0.5, 0.75):
            y = rect.y + rect.h * frac
            ctx.beginPath()
            ctx.moveTo(0, y)
            ctx.lineTo(w, y)
            ctx.stroke()

        if samples:
            lo, hi = value_range(samples, z)
            if sig.high_limit is not None and lo <= sig.high_limit <= hi * 1.15:
                hi = max(hi, sig.high_limit)
            proj = Projector(Rect(rect.x + 2, rect.y + 4, rect.w - 4, rect.h - 8), t0, t1, lo, hi)

            ctx.fillStyle = "rgba(131,150,168,0.13)"
            ctx.fill(window.Path2D.new(band_path(samples, proj, z)))

            if sig.high_limit is not None and lo <= sig.high_limit <= hi:
                y = proj.y(sig.high_limit)
                ctx.save()
                ctx.setLineDash([4, 4])
                ctx.strokeStyle = "rgba(239,68,68,0.55)"
                ctx.beginPath()
                ctx.moveTo(0, y)
                ctx.lineTo(w, y)
                ctx.stroke()
                ctx.restore()

            ctx.strokeStyle = color
            ctx.lineWidth = 1.6
            ctx.lineJoin = "round"
            ctx.stroke(window.Path2D.new(series_path(samples, proj)))

            pts = alert_points(samples, proj)
            for level, fill in LEVEL_COLORS.items():
                d = dots_path(((x, y) for x, y, lv in pts if lv == level), 2.4)
                if d:
                    ctx.fillStyle = fill
                    ctx.fill(window.Path2D.new(d))

            ctx.fillStyle = TEXT_MUTED
            ctx.textAlign = "right"
            ctx.fillText(format_value(hi), w - 4, rect.y + 11)
            ctx.fillText(format_value(lo), w - 4, rect.bottom - 4)

        ctx.textAlign = "left"
        ctx.fillStyle = color
        ctx.fillText(f"{sig.name} ({sig.unit})", 6, rect.y + 12)


def render_tiles() -> None:
    plant = state.plant
    for mid, view in state.views.items():
        status = plant.status(mid).value
        if status != view.last_status:
            view.card.setAttribute("data-status", status)
            view.pill.className = f"pill pill-{status.lower()}"
            view.pill.textContent = status
            view.last_status = status
        latest = plant.latest(mid)
        for name, (box, v, unit) in view.readouts.items():
            s = latest[name]
            if s is None:
                continue
            v.innerHTML = f'{format_value(s.value)}<span class="u">{unit}</span>'
            box.setAttribute("data-level", str(int(plant.detectors[mid][name].level)))
        faults = plant.faults(mid)
        view.faults.textContent = (
            "⚠ " + ", ".join(f.value.replace("_", " ") for f in faults) if faults else ""
        )
        draw_machine(mid, view)


def render_stats() -> None:
    plant = state.plant
    s = plant.stats
    el("clock").textContent = format_clock(plant.now)
    el("st-ticks").textContent = str(s.ticks)
    el("st-sent").textContent = str(s.sent)
    el("st-ooo").textContent = str(s.out_of_order)
    el("st-late").textContent = f"{s.late} / {s.skipped}"
    el("st-depth").textContent = str(sum(b.depth for b in plant.buffers.values()))
    el("st-alerts").textContent = str(plant.active_alerts())


def append_events(events: list[Event]) -> None:
    log = el("log")
    empty = log.querySelector(".empty")
    if events and empty:
        empty.remove()
    for ev in events:
        li = make("li")
        li.append(make("span", "ts", format_clock(ev.ts)))
        li.append(make("span", f"tag tag-{ev.severity.value.lower()}", ev.severity.value))
        li.append(make("span", "mid", ev.machine_id))
        li.append(make("span", "msg", ev.message))
        log.prepend(li)
    while log.children.length > MAX_LOG_ITEMS:
        log.lastElementChild.remove()


def reset_log() -> None:
    log = el("log")
    log.innerHTML = ""
    log.append(make("li", "empty", "No events yet — inject a fault above."))


def render() -> None:
    if state.pending_events:
        append_events(state.pending_events)
        state.pending_events = []
    render_tiles()
    render_stats()


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #


def set_running(running: bool) -> None:
    state.running = running
    el("btn-toggle").textContent = "Pause" if running else "Start"
    pill = el("run-state")
    pill.textContent = "RUNNING" if running else "PAUSED"
    pill.className = "pill " + ("pill-ok" if running else "pill-stale")


def flash(btn) -> None:
    btn.classList.add("flash")
    window.setTimeout(create_once_callable(lambda: btn.classList.remove("flash")), 400)


def on_fault(evt) -> None:
    btn = evt.currentTarget
    kind = FaultKind(btn.dataset.fault)
    state.pending_events.append(state.plant.inject(state.target, kind))
    flash(btn)
    render()


def on_clear(evt) -> None:
    state.pending_events.append(state.plant.clear_faults(state.target))
    flash(evt.currentTarget)
    render()


def on_reset(_evt) -> None:
    state.plant.reset()
    for view in state.views.values():
        view.last_status = ""
    reset_log()
    render()


def on_slider(id_: str, out_id: str, apply, fmt=str):
    def handler(_evt):
        raw = el(id_).value
        apply(raw)
        el(out_id).textContent = fmt(raw)
        render()
    return handler


def on_key(evt) -> None:
    tag = (evt.target.tagName or "").lower()
    if evt.key == " " and tag not in ("input", "select", "button", "textarea"):
        evt.preventDefault()
        set_running(not state.running)


# Source viewer: fetches the very files this page is running.
_source_cache: dict[str, str] = {}


async def show_source(name: str) -> None:
    code = el("source-code")
    for tab in document.querySelectorAll(".tab"):
        tab.classList.toggle("active", tab.dataset.file == name)
    if name not in _source_cache:
        code.textContent = "Loading…"
        try:
            resp = await pyfetch(f"./{name}")
            _source_cache[name] = await resp.string() if resp.ok else f"HTTP {resp.status}"
        except Exception as exc:  # network errors on file:// etc.
            _source_cache[name] = f"Could not load {name}: {exc}"
    code.textContent = _source_cache[name]
    code.scrollTop = 0


def open_source(evt) -> None:
    evt.preventDefault()
    el("source-dialog").showModal()
    asyncio.ensure_future(show_source("sim.py"))


def wire_controls() -> None:
    cfg = state.plant.cfg

    def set_z(v): cfg.z_warn = float(v)
    def set_window(v): cfg.window = int(v)
    def set_debounce(v): cfg.on_count = int(v)

    on(el("btn-toggle"), "click", lambda e: set_running(not state.running))
    on(el("btn-reset"), "click", on_reset)
    on(el("speed"), "change", lambda e: setattr(state, "speed", float(el("speed").value)))
    on(el("z"), "input", on_slider("z", "z-out", set_z, lambda v: f"{float(v):.1f}"))
    on(el("window"), "input", on_slider("window", "win-out", set_window))
    on(el("debounce"), "input", on_slider("debounce", "deb-out", set_debounce))
    on(el("target"), "change", lambda e: set_target(el("target").value))
    for btn in document.querySelectorAll(".fault-btn"):
        on(btn, "click", on_fault)
    on(el("btn-clear"), "click", on_clear)
    on(el("btn-clear-log"), "click", lambda e: reset_log())
    on(document, "keydown", on_key)

    on(el("view-source"), "click", open_source)
    on(el("close-source"), "click", lambda e: el("source-dialog").close())
    for tab in document.querySelectorAll(".tab"):
        on(tab, "click", lambda e: asyncio.ensure_future(show_source(e.currentTarget.dataset.file)))

    repo = document.querySelector('meta[name="repo-url"]')
    if repo and repo.content:
        link = el("repo-link")
        link.href = repo.content
        link.classList.remove("hidden")

    for node in document.querySelectorAll("button, select, input"):
        node.disabled = False


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


async def main_loop() -> None:
    last = window.performance.now()
    budget = 0.0
    while True:
        await asyncio.sleep(FRAME_SECONDS)
        now = window.performance.now()
        dt = min((now - last) / 1000, 1.0)  # clamp: background tabs get throttled
        last = now
        if state.running:
            budget += dt * BASE_TICKS_PER_SEC * state.speed
            steps, budget = int(budget), budget - int(budget)
            for _ in range(steps):
                state.pending_events.extend(state.plant.tick())
        try:
            render()
        except Exception as exc:  # never let one bad frame kill the loop
            window.console.error(f"render failed: {exc!r}")


build_machines()
wire_controls()
reset_log()
state.plant.run(40)  # a little history so charts aren't empty on first paint
set_running(True)
render()
document.body.classList.add("py-ready")
window.console.log("Factory Telemetry Simulator: Python ready")
asyncio.ensure_future(main_loop())
