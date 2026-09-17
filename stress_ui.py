#!/usr/bin/env python3
"""stress_ui.py - a Tkinter control panel for putting the demo app's endpoints under load.

Stdlib only. No pip install, no server, no compose changes.

    python stress_ui.py
    python stress_ui.py --target http://localhost:8000

Every endpoint gets its own row where you choose:

    Workers      how many requests may be in flight at once
    Target req/s 0 = go as fast as the workers can; >0 = paced to that rate
    Duration     0 = run until you press Stop; >0 = stop automatically

and the row then reports its *stress state* - achieved req/s, total requests,
errors, and average/max latency.

A note on the "err" column: anything that is not a 2xx counts, so /fail (always
500) and /nope (always 404) will legitimately sit near 100% errors. That is the
demo service behaving exactly as designed, not a fault in this tool.

This pairs with the Prometheus + Grafana stack in this repo. Start a load here,
then watch the matching panels in Grafana about 15s later - that scrape-interval
lag is the pull model made visible.
"""

from __future__ import annotations

import argparse
import http.client
import threading
import time
import tkinter as tk
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk
from urllib.parse import urlsplit

# The endpoints app/app.py serves. /fail and /nope are deliberate error routes.
ENDPOINTS = ["/", "/healthz", "/work", "/slow", "/fail", "/nope"]

DEFAULT_TARGET = "http://localhost:8000"

# Two timeouts, deliberately different. A wrong host or a stopped stack should
# say so within seconds - but /slow can legitimately take seconds under load, so
# the read side stays generous. Collapsing these into one value means either
# false errors on slow endpoints, or 30 seconds of silence on a typo'd URL.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 30.0

RATE_WINDOW_SECONDS = 5.0     # window used to compute the displayed req/s
MAX_WORKERS = 512             # hard cap so a typo cannot fork-bomb the machine


class RateWindow:
    """Sliding-window request counter -> achieved requests/second."""

    def __init__(self, seconds: float = RATE_WINDOW_SECONDS) -> None:
        self.seconds = seconds
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def add(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._times.append(now)
            self._prune(now)

    def rate(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return len(self._times) / self.seconds

    def _prune(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._times and self._times[0] < cutoff:
            self._times.popleft()


class EndpointLoad:
    """A start/stoppable load generator targeting a single URL path."""

    def __init__(self, base_url: str, path: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "idle"          # idle | running | stopping | finished
        self._window = RateWindow()
        self._reset_counters()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int, rps: float, duration: int) -> None:
        if self.running:
            return
        with self._lock:
            self._reset_counters()
        self._window = RateWindow()
        self._stop.clear()
        self._state = "running"
        self._thread = threading.Thread(
            target=self._supervise, args=(workers, rps, duration),
            name=f"load{self.path}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self.running:
            self._state = "stopping"
            self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            total = self._total
            errors = self._errors
            latency_sum = self._latency_sum
            latency_max = self._latency_max
        state = self._state
        if state == "running" and self._stop.is_set():
            state = "stopping"
        return {
            "state": state,
            "total": total,
            "errors": errors,
            "avg_ms": (latency_sum / total * 1000.0) if total else 0.0,
            "max_ms": latency_max * 1000.0,
            "rps": self._window.rate(),
        }

    # -- internals ---------------------------------------------------------

    def _reset_counters(self) -> None:
        self._total = 0
        self._errors = 0
        self._latency_sum = 0.0
        self._latency_max = 0.0

    def _supervise(self, workers: int, rps: float, duration: int) -> None:
        """Dispatch requests at the requested rate, bounded by `workers` in flight."""
        sem = threading.BoundedSemaphore(workers)
        interval = (1.0 / rps) if rps and rps > 0 else None
        deadline = (time.monotonic() + duration) if duration and duration > 0 else None
        next_at = time.perf_counter()

        def one_request() -> None:
            try:
                self._do_request()
            finally:
                sem.release()

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while not self._stop.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    # Acquire a slot *before* submitting, so we never queue an
                    # unbounded backlog of work that Stop would then have to drain.
                    if not sem.acquire(timeout=0.2):
                        continue
                    pool.submit(one_request)
                    if interval is not None:
                        next_at += interval
                        delay = next_at - time.perf_counter()
                        if delay > 0:
                            if self._stop.wait(min(delay, 0.25)):
                                break
                        else:
                            # We are behind the requested rate. Resync instead of
                            # accumulating a backlog we can never catch up on.
                            next_at = time.perf_counter()
        finally:
            self._state = "finished"

    def _do_request(self) -> None:
        parts = urlsplit(self.base_url)
        started = time.perf_counter()
        status: int | None = None
        transport_error = False
        connection = None
        try:
            # A fresh connection per request, so the load reflects real
            # connection setup and one wedged socket cannot block the next.
            if parts.scheme == "https":
                connection = http.client.HTTPSConnection(
                    parts.hostname, parts.port or 443, timeout=CONNECT_TIMEOUT)
            else:
                connection = http.client.HTTPConnection(
                    parts.hostname, parts.port or 80, timeout=CONNECT_TIMEOUT)
            connection.connect()
            # Connected - relax the socket for the response itself.
            connection.sock.settimeout(READ_TIMEOUT)
            connection.request("GET", self.path)
            response = connection.getresponse()
            status = response.status
            response.read()
        except Exception:
            transport_error = True
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        elapsed = time.perf_counter() - started

        with self._lock:
            self._total += 1
            if transport_error or (status is not None and status >= 400):
                self._errors += 1
            self._latency_sum += elapsed
            if elapsed > self._latency_max:
                self._latency_max = elapsed
        self._window.add()


def read_number(var: tk.Variable, cast, default, low, high):
    """Read a Spinbox variable defensively - partial text raises TclError."""
    try:
        value = cast(var.get())
    except (tk.TclError, ValueError, TypeError):
        return default
    if isinstance(value, float) and value != value:      # NaN
        return default
    return max(low, min(high, value))


class StressUI:
    def __init__(self, root: tk.Tk, target: str) -> None:
        self.root = root
        self.root.title("Endpoint stress panel  -  Prometheus / Grafana demo")
        self.root.minsize(1020, 380)

        self.target = tk.StringVar(value=target)
        self.loads: dict[str, EndpointLoad] = {}
        self.rows: dict[str, dict] = {}
        self._closed = False

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._check_target)
        self.root.after(500, self._refresh)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # --- target -------------------------------------------------------
        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Target").grid(row=0, column=0, padx=(0, 8))
        entry = ttk.Entry(top, textvariable=self.target, width=42)
        entry.grid(row=0, column=1, sticky="w")
        entry.bind("<FocusOut>", lambda _e: self._retarget())
        entry.bind("<Return>", lambda _e: self._retarget())
        self.target_hint = ttk.Label(top, text="checking...", foreground="#666")
        self.target_hint.grid(row=0, column=2, padx=12)

        ttk.Separator(outer).grid(row=1, column=0, sticky="ew", pady=10)

        # --- column headers ----------------------------------------------
        table = ttk.Frame(outer)
        table.grid(row=2, column=0, sticky="ew")
        table.columnconfigure(5, weight=1)
        headers = ["Endpoint", "Workers", "Target req/s", "Duration (s)", "", "Stress state"]
        for col, text in enumerate(headers):
            ttk.Label(table, text=text, font=("", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=(0, 10), pady=(0, 6))

        # --- one row per endpoint -----------------------------------------
        for index, path in enumerate(ENDPOINTS, start=1):
            self.loads[path] = EndpointLoad(self.target.get(), path)

            workers = tk.IntVar(value=4)
            rps = tk.DoubleVar(value=0.0)
            duration = tk.IntVar(value=0)

            ttk.Label(table, text=path, font=("Consolas", 10)).grid(
                row=index, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Spinbox(table, from_=1, to=MAX_WORKERS, textvariable=workers,
                        width=7).grid(row=index, column=1, sticky="w", padx=(0, 10))
            ttk.Spinbox(table, from_=0, to=100000, increment=5, textvariable=rps,
                        width=11).grid(row=index, column=2, sticky="w", padx=(0, 10))
            ttk.Spinbox(table, from_=0, to=86400, increment=10, textvariable=duration,
                        width=11).grid(row=index, column=3, sticky="w", padx=(0, 10))

            button = ttk.Button(table, text="Start", width=8,
                                command=lambda p=path: self._toggle(p))
            button.grid(row=index, column=4, sticky="w", padx=(0, 12))

            status = ttk.Label(table, text="idle - not started", font=("Consolas", 9))
            status.grid(row=index, column=5, sticky="w")

            self.rows[path] = {"workers": workers, "rps": rps, "duration": duration,
                               "button": button, "status": status}

        # --- footer --------------------------------------------------------
        ttk.Separator(outer).grid(row=3, column=0, sticky="sew", pady=(10, 0))
        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew")
        ttk.Button(footer, text="Start all", command=self._start_all).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(footer, text="Stop all", command=self._stop_all).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(
            footer,
            text="/fail (500) and /nope (404) report as errors by design.  "
                 "Graphs in Grafana lag by up to one scrape interval (15s).",
            foreground="#666",
        ).grid(row=0, column=2, sticky="w")

    # -- actions -----------------------------------------------------------

    def _retarget(self) -> None:
        base = self.target.get().strip().rstrip("/") or DEFAULT_TARGET
        self.target.set(base)
        for load in self.loads.values():
            load.base_url = base
        self._check_target()

    def _toggle(self, path: str) -> None:
        load = self.loads[path]
        if load.running:
            load.stop()
            return
        widgets = self.rows[path]
        load.base_url = self.target.get().strip().rstrip("/") or DEFAULT_TARGET
        load.start(
            workers=read_number(widgets["workers"], int, 4, 1, MAX_WORKERS),
            rps=read_number(widgets["rps"], float, 0.0, 0.0, 1.0e9),
            duration=read_number(widgets["duration"], int, 0, 0, 86400),
        )

    def _start_all(self) -> None:
        for path in ENDPOINTS:
            if not self.loads[path].running:
                self._toggle(path)

    def _stop_all(self) -> None:
        for load in self.loads.values():
            load.stop()

    def _check_target(self) -> None:
        """One quick request in the background so we do not block the UI."""
        base = self.target.get().strip().rstrip("/") or DEFAULT_TARGET

        def probe() -> None:
            message, colour = "unreachable", "#c0392b"
            for path in ("/healthz", "/"):
                try:
                    with urllib.request.urlopen(base + path, timeout=2) as response:
                        if response.status < 400:
                            message, colour = f"reachable ({path} -> {response.status})", "#1e8449"
                            break
                except Exception:
                    continue
            self._post_to_ui(
                lambda: self.target_hint.configure(text=message, foreground=colour))

        threading.Thread(target=probe, daemon=True).start()

    def _post_to_ui(self, callback) -> None:
        """Schedule a widget update from a worker thread, tolerating shutdown.

        Tkinter is not thread-safe, and calling root.after() on a destroyed
        window raises "main thread is not in main loop" - which is exactly what
        happens if the window is closed while a probe is still in flight.
        """
        if self._closed:
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass          # the window went away between the check and the call

    # -- periodic redraw ---------------------------------------------------

    def _refresh(self) -> None:
        if self._closed:
            return
        for path, load in self.loads.items():
            widgets = self.rows[path]
            snap = load.snapshot()
            widgets["button"].configure(text="Stop" if load.running else "Start")
            if snap["state"] == "idle":
                widgets["status"].configure(text="idle - not started")
            else:
                widgets["status"].configure(text=(
                    f"{snap['state']:<9} {snap['rps']:8.1f} req/s "
                    f"{snap['total']:>9,} req {snap['errors']:>8,} err "
                    f"{snap['avg_ms']:7.1f} ms avg {snap['max_ms']:8.1f} ms max"
                ))
        self.root.after(500, self._refresh)

    def _on_close(self) -> None:
        self._closed = True
        for load in self.loads.values():
            load.stop()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help=f"base URL of the demo service (default: {DEFAULT_TARGET})")
    args = parser.parse_args()

    root = tk.Tk()
    StressUI(root, args.target)
    root.mainloop()


if __name__ == "__main__":
    main()
