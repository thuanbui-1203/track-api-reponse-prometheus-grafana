"""Tiny load generator so the dashboards have data the moment the stack is up.

Without traffic every rate() is 0 and the graphs look broken, which is a
confusing first impression. This hits a mix of endpoints - including the ones
that intentionally return 404/500 - so the error-rate panels have something to
show too.

Run locally:  TARGET=http://localhost:8000 python loadgen.py
"""

from __future__ import annotations

import os
import random
import time
import urllib.error
import urllib.request

TARGET = os.environ.get("TARGET", "http://localhost:8000")

# Weighted by repetition: mostly happy-path traffic, a little bit of failure.
PATHS = ["/", "/healthz", "/work", "/work", "/work", "/work", "/slow", "/fail", "/nope"]


def hit(path: str) -> None:
    try:
        with urllib.request.urlopen(TARGET + path, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError:
        pass  # 404/500 from /fail and /nope are expected and are the point
    except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
        print(f"[loadgen] {path}: {exc}", flush=True)
        time.sleep(2)


def main() -> None:
    print(f"[loadgen] target {TARGET}", flush=True)
    while True:
        hit(random.choice(PATHS))
        time.sleep(random.uniform(0.05, 0.5))


if __name__ == "__main__":
    main()
