"""Demo service instrumented with the Prometheus Python client.

Everything Prometheus knows about this process comes from the /metrics
endpoint below. The app is deliberately tiny: a stdlib HTTP server plus a
handful of metric objects, so the interesting part (how the four metric
types behave) is easy to see.

Run locally:   python app.py
Inspect:       curl http://localhost:8000/metrics
"""

from __future__ import annotations

import os
import random
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

PORT = int(os.environ.get("PORT", "8000"))
VERSION = os.environ.get("APP_VERSION", "1.0.0")

# ---------------------------------------------------------------------------
# Metric definitions
#
# Naming convention: <namespace>_<subsystem>_<name>_<unit>. Prometheus never
# rewrites names, so getting the convention right is worth the two seconds:
#   * counters end in _total
#   * histograms expose _bucket / _sum / _count
#   * units are base units (seconds, bytes) and go at the end
# ---------------------------------------------------------------------------

# COUNTER - monotonically increasing. Resets to 0 only when the process
# restarts, which is why you always query counters through rate()/increase()
# rather than reading the raw value.
REQUESTS = Counter(
    "demo_http_requests_total",
    "Total HTTP requests handled.",
    ["method", "path", "status"],
)

# HISTOGRAM - observations bucketed into cumulative ranges. This is what backs
# latency percentiles via histogram_quantile(), and it is the metric type that
# is cheapest to aggregate across instances.
LATENCY = Histogram(
    "demo_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# GAUGE - a value that goes up and down. Never use rate() on a gauge.
IN_PROGRESS = Gauge(
    "demo_http_requests_in_progress",
    "HTTP requests currently being handled.",
)

QUEUE_DEPTH = Gauge(
    "demo_queue_depth",
    "Jobs waiting in the simulated queue.",
)

# A counter is also the idiomatic way to export multi-dimensional facts:
# label it with the outcome you care about instead of making N separate metrics.
JOBS = Counter(
    "demo_jobs_processed_total",
    "Background jobs processed.",
    ["outcome"],
)

# The "info" metric pattern: a gauge that is permanently 1, carrying version
# metadata in its labels. Lets you join deployment info onto any other series.
BUILD_INFO = Gauge(
    "demo_build_info",
    "Build metadata. The value is always 1.",
    ["version"],
)
BUILD_INFO.labels(version=VERSION).set(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "demo-app/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - name is fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]

        # /metrics is scraped by Prometheus itself. Instrumenting it would
        # create a feedback loop where scrape traffic dominates the graphs,
        # so it is handled first and never recorded.
        if path == "/metrics":
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        IN_PROGRESS.inc()
        started = time.perf_counter()
        try:
            status, body = self._route(path)
        except Exception as exc:  # noqa: BLE001 - a bug must not kill the request loop
            status, body = 500, f"unhandled error: {exc}\n"
        finally:
            # Decrement on every path, including failures. A gauge that only
            # goes down on the happy path creeps upward forever and the panel
            # becomes worse than useless.
            IN_PROGRESS.dec()

        LATENCY.labels(path=path).observe(time.perf_counter() - started)
        REQUESTS.labels(method="GET", path=path, status=str(status)).inc()

        self._respond(status, body)

    def _route(self, path: str) -> Tuple[int, str]:
        if path == "/":
            return 200, "demo app - metrics at /metrics\n"
        if path == "/healthz":
            return 200, "ok\n"
        if path == "/work":
            # A unit of work with deliberately variable duration, so the
            # latency histogram has a spread worth looking at.
            time.sleep(random.uniform(0.01, 0.4))
            outcome = "ok" if random.random() < 0.9 else "error"
            JOBS.labels(outcome=outcome).inc()
            return (200 if outcome == "ok" else 500), f"work done ({outcome})\n"
        if path == "/slow":
            time.sleep(random.uniform(1.0, 3.0))
            return 200, "slow request finished\n"
        if path == "/fail":
            return 500, "intentional failure\n"
        return 404, "not found\n"

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        # Keep container logs readable. Prometheus scrapes /metrics every 15s,
        # so logging it would bury real traffic in scrape noise.
        # getattr, not self.requestline: log_message is also reached from
        # log_error on paths where parse_request() never ran (e.g. a timeout
        # while reading the request line), and the attribute is only set there.
        if "/metrics" in getattr(self, "requestline", ""):
            return
        print(f"[app] {self.address_string()} {fmt % args}", flush=True)


def background_worker(stop_event: threading.Event) -> None:
    """Simulated queue so gauges move even when nobody is sending traffic."""
    tick = 0
    while not stop_event.wait(1.0):
        tick += 1
        # A slow sinusoid plus noise - looks like a real queue, not a random walk.
        QUEUE_DEPTH.set(max(0.0, 6 + 5 * (tick % 60) / 60 + random.uniform(-2, 2)))
        JOBS.labels(outcome="ok").inc(random.randint(0, 4))


def main() -> None:
    stop_event = threading.Event()
    threading.Thread(
        target=background_worker, args=(stop_event,), daemon=True, name="worker"
    ).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def shutdown(signum, frame):  # noqa: ANN001, ARG001
        print(f"[app] signal {signum} received, shutting down", flush=True)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"[app] listening on :{PORT} (version {VERSION})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
