# Prometheus + Grafana, end to end

A working, self-contained example: a small Python service exposes metrics, Prometheus
scrapes and stores them, Grafana draws them. Plus the concepts behind each piece, so you
can read the config files and know *why* they look the way they do.

The whole stack is defined in this repo. Nothing here is a screenshot of someone else's
dashboard — when you run it, every number you see comes from the app in `app/app.py`.

> **Companion doc:** [`PROMQL.md`](PROMQL.md) is a standalone PromQL cookbook — the queries
> you actually reach for, the mistakes that bite, and an appendix mapping every example to
> this project's `demo_*` metrics.

---

## 0. Quick start

```bash
docker compose up -d --build
```

Wait ~30 seconds, then:

| URL | What it is | Login |
|---|---|---|
| http://localhost:8000/metrics | The raw metrics the app exposes | — |
| http://localhost:9090 | Prometheus UI (query + targets) | — |
| http://localhost:3000 | Grafana (dashboard already loaded) | `admin` / `admin` |

Useful follow-ups:

```bash
docker compose ps                 # are all four containers up?
docker compose logs -f app        # what the app is doing
docker compose down               # stop, keep data
docker compose down -v            # stop, wipe stored metrics
```

> First run builds an image and pulls two more (~500 MB), so it takes a few minutes.
> Later runs start in seconds.

---

## 1. The mental model

Almost every misunderstanding of Prometheus comes from missing one design decision:
**Prometheus pulls, it does not receive.**

```
        pull /metrics every 15s
        ─────────────────────────►
Prometheus                      Your app
  ├─ TSDB (stores samples)        └─ exposes an HTTP text page
  └─ HTTP API for queries
        ▲
        │ PromQL over HTTP
        │
    Grafana  ──►  your browser
```

Consequences worth internalising, because they explain most of the design:

- **Your app needs no agent.** It just serves an HTTP endpoint.
- **Your app doesn't need to know Prometheus exists.** It can't be "down" from the app's
  perspective; if Prometheus is down, the app keeps serving metrics and nothing is lost
  except the samples nobody collected.
- **Targets must be discoverable.** Prometheus needs a list of things to scrape — that's
  what `scrape_configs` in `prometheus/prometheus.yml` is.
- **You find out that a target is dead via the `up` metric**, not via an error. If a scrape
  fails, Prometheus records `up = 0` and carries on.
- **Sampling is periodic.** You get a point every 15s. Sub-15s spikes are invisible, and no
  query can recover them. Set `scrape_interval` based on the fastest thing you need to see.

Grafana's role is narrower than people expect: **it stores nothing**. It sends a query,
Prometheus answers with numbers, Grafana draws them. Change the dashboard time range and
Grafana re-queries. All the data lives in Prometheus's TSDB.

---

## 2. The four metric types

You only get four. Choosing the right one is most of the skill.

| Type | Semantics | Use for | Query with |
|---|---|---|---|
| **Counter** | Only ever goes up (resets to 0 on restart) | Requests served, errors, bytes sent | `rate()` / `increase()` |
| **Gauge** | Goes up and down | Queue depth, memory in use, connections | raw value |
| **Histogram** | Counts observations into buckets | Latencies, request/response sizes | `histogram_quantile()` |
| **Summary** | Client-side quantiles | Rarely — see below | the exported quantiles |

In `app/app.py` you can see all four:

```python
REQUESTS   = Counter("demo_http_requests_total", ..., ["method", "path", "status"])
LATENCY    = Histogram("demo_http_request_duration_seconds", ..., ["path"], buckets=(...))
IN_PROGRESS = Gauge("demo_http_requests_in_progress", ...)
QUEUE_DEPTH = Gauge("demo_queue_depth", ...)
JOBS       = Counter("demo_jobs_processed_total", ..., ["outcome"])
```

### The counter rule

Never plot a raw counter. `demo_http_requests_total = 41_872` is meaningless; it only ever
grows. What you want is *per second*:

```
rate(demo_http_requests_total[1m])    # samples/sec over the last minute
```

`rate()` also handles the restart case for free: if the counter resets mid-window, `rate()`
assumes the process restarted and compensates. That's exactly why you're told to use it
rather than subtracting two samples yourself.

### Histograms vs. summaries

A Histogram export looks like three metrics:

```
demo_http_request_duration_seconds_bucket{le="0.1"}  1203
demo_http_request_duration_seconds_bucket{le="0.25"} 1290
demo_http_request_duration_seconds_bucket{le="+Inf"} 1300
demo_http_request_duration_seconds_sum                86.4
demo_http_request_duration_seconds_count              1300
```

The `le` label ("less than or equal") gives cumulative bucket counts. Because the data is
just counters, you can **aggregate histograms across instances** — `sum by (le)` — and
still get correct percentiles. That is the property Summaries lack: a Summary computes its
quantiles in the client and exports opaque numbers, so `sum()` across three replicas gives
you meaningless arithmetic. Prefer Histograms unless you have a specific reason not to.

The trade-off is **bucket boundaries are fixed at declaration time**. If your p99 is 3.1s
and your largest bucket is 1s, everything lands in `+Inf` and the quantile is a lie.

### Naming

`<namespace>_<subsystem>_<name>_<unit>`. Counters end in `_total`, units are base units
(seconds, not milliseconds) and go last. Prometheus will not rename anything for you.

---

## 3. Labels are the whole game

Labels turn one metric into many time series. Same counter, three questions:

```promql
sum by (path)   (rate(demo_http_requests_total[1m]))   # which endpoint is hot?
sum by (status) (rate(demo_http_requests_total[1m]))   # how many errors?
sum             (rate(demo_http_requests_total[1m]))   # total throughput
```

Each distinct label combination is a **separate series**, stored separately, costing
separate memory. `status` has ~5 values, `path` ~6, `method` 1 — so this metric is ~30
series. Fine.

Now imagine putting `user_id` on it. Or `request_id`. Or the raw URL with the query string.
Each unique value is a new series, forever, for as long as the retention window. This is
**cardinality explosion**, and it is the number one way people kill a Prometheus. Rule of
thumb: **labels should have bounded, low-cardinality values**, and never contain user data.

If you need high-cardinality detail, put it in logs or traces (Loki / Tempo), and keep
Prometheus for aggregates.

---

## 4. What's in this repo

```
prometheus_grafana/
├── docker-compose.yml              # the four services
├── app/
│   ├── app.py                      # instrumented service  → /metrics
│   ├── loadgen.py                  # generates traffic so graphs aren't flat
│   ├── requirements.txt            # prometheus-client==0.23.1
│   └── Dockerfile
├── prometheus/
│   └── prometheus.yml              # what to scrape, and how often
└── grafana/
    ├── provisioning/
    │   ├── datasources/prometheus.yml   # "connect Grafana to Prometheus"
    │   └── dashboards/default.yml       # "load every JSON in a folder"
    └── dashboards/demo-app.json         # the actual dashboard
```

Scrape config, annotated:

```yaml
global:
  scrape_interval: 15s        # data resolution. also your rate() floor.
  evaluation_interval: 15s    # how often alerting rules run

scrape_configs:
  - job_name: prometheus      # a "job" is a group scraped the same way
    static_configs:
      - targets: ["localhost:9090"]      # yes, Prometheus scrapes itself

  - job_name: demo-app
    metrics_path: /metrics
    static_configs:
      - targets: ["app:8000"]            # compose service name == hostname
        labels:
          env: demo                      # attached to every series from here
```

`static_configs` is the simplest target discovery. The real world uses
`file_sd_configs` or service discovery (Kubernetes, EC2) so targets appear and disappear
without editing YAML.

---

## 5. PromQL from zero

PromQL is small. Four ideas cover the vast majority of real queries.

### (a) Select series — instant vectors

```promql
demo_queue_depth                              # the series named exactly this
demo_queue_depth{job="demo-app"}              # filtered by label
demo_http_requests_total{status=~"5.."}       # regex match (=~, !~)
demo_http_requests_total{status!="200"}       # not-equal
```

The metric name is itself just a label (`__name__`). `{status="500"}` with no metric name
is legal and matches *everything* with that label — useful, but expensive.

### (b) Ranges and rates

A `[5m]` suffix means "the last 5 minutes of samples" — a **range vector**, which is not a
number and cannot be graphed directly.

```promql
rate(demo_http_requests_total[5m])        # per-second average rate
increase(demo_http_requests_total[5m])    # total increase over the window
irate(demo_http_requests_total[5m])       # uses only the last 2 samples — spiky
```

**The window must contain at least 2 samples**, so with a 15s scrape interval anything
shorter than ~30s gives empty results. `irate` is twitchy but reacts fast; `rate` is
smoother. Use `rate` unless you specifically need edge detection.

### (c) Aggregate and group

```promql
sum(rate(demo_http_requests_total[1m]))                     # one number
sum by (path)   (rate(demo_http_requests_total[1m]))        # split by path
sum without (method) (rate(demo_http_requests_total[1m]))   # keep all but method
avg, min, max, count, topk, quantile                        # all take by/without
```

### (d) Percentiles from a histogram

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m]))
)
```

`by (le)` is not optional. Without it the function can't see the bucket structure and
returns garbage. Read it as: *aggregate all buckets, then interpolate within the bucket
that contains the 95th percentile.* Bucket interpolation is why p99 from a Histogram is an
estimate, not an exact measurement.

### Putting it together: error rate

```promql
100 * sum(rate(demo_http_requests_total{status=~"5.."}[5m]))
    / sum(rate(demo_http_requests_total[5m]))
```

This is on panel 2 of the dashboard (with a `clamp_min()` guard so the empty-traffic case
returns 0 instead of `NaN`). Three habits are visible in it:

1. **Numerator and denominator use the same range** — mismatched windows give nonsense.
2. **Filter with a label matcher**, not by multiple queries.
3. **Always define the denominator's zero case.** A bare division by zero yields `NaN` and
   the panel goes blank, which looks like an outage.

Gotchas that bite everyone:

- **Instant vector vs. range vector** — `rate()` and friends take a range vector;
  almost everything else takes an instant vector. Mixing them up is the usual parse error.
- **Empty result ≠ zero.** "No 5xx in the window" and "no traffic at all" both return an
  empty vector. If zeros matter, wrap in `or vector(0)`.
- `offset 1w`, `@ end()`, and subqueries `[5m:1m]` exist when you need them.

---

## 6. Grafana

### Datasource

Grafana must be told where Prometheus is. In this repo that's declared in
`grafana/provisioning/datasources/prometheus.yml` rather than clicked into the UI:

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus             # ← important, see below
    url: http://prometheus:9090 # compose service name, NOT localhost
    isDefault: true
    jsonData:
      timeInterval: 15s         # match your scrape_interval
```

Two things worth calling out:

- **`url: http://prometheus:9090`.** Grafana runs in its own container; for it,
  `localhost` is itself, not the host. Docker Compose puts all services on one network and
  makes the service name a DNS entry — that's the entire mechanism.
- **Set `uid` explicitly.** Leave it out and Grafana generates a random UID; every
  provisioned dashboard that hard-codes `"uid": "prometheus"` then fails to resolve its
  datasource. This is the single most common provisioning bug.

### Provisioning vs. clicking

You *can* add all of this through the UI. Provisioning files exist because:

- the setup is **reproducible** — `docker compose up` on a fresh machine gives the same
  result, no tribal knowledge;
- it is **reviewable** — datasource and dashboard changes go through code review like
  everything else;
- it is **recoverable** — lose the Grafana volume and you lose nothing.

`provisioning/dashboards/default.yml` tells Grafana to treat a directory as the source of
truth and rescan it every 30s, so editing `demo-app.json` and waiting a moment is enough.

### Anatomy of a panel

Every panel in Grafana is really four things:

| Part | Where in the JSON | What it does |
|---|---|---|
| **Query** | `targets[]` | the PromQL, plus the datasource |
| **Transform** | `transformations[]` | client-side reshaping (often unnecessary) |
| **Visualisation** | `type`, `options` | timeseries / stat / table / gauge / heatmap |
| **Field config** | `fieldConfig.defaults` | units, thresholds, colour, overrides |

The `unit` setting is the one people skip and then regret: with `unit: "s"` Grafana
auto-scales to `ms`/`µs`; with `unit: "reqps"` it renders `1.5 K req/s` for you. Without it
you get raw numbers and have to do mental arithmetic forever.

`fieldConfig.overrides[]` is how you make exceptions per-series — the dashboard uses it to
colour any series matching `5..` red, and to make the `error` outcome red:

```json
{
  "matcher": { "id": "byRegexp", "options": "5.." },
  "properties": [{ "id": "color", "value": { "fixedColor": "red", "mode": "fixed" } }]
}
```

### A pattern worth stealing

Dashboard JSON is verbose and unpleasant to hand-edit — which is exactly why the workflow
is: **build the panel in the UI, then Export → Save to file.** Click around in Grafana,
get it right visually, then commit the exported JSON. Hand-writing it, as this repo does
for the initial version, is fine once and miserable as a habit.

---

## 7. A guided tour

Work through these in order; each one makes a specific point.

1. **See the raw data.**
   ```bash
   curl http://localhost:8000/metrics | head -40
   ```
   This is the *entire* interface between your app and Prometheus. Plain text, one line per
   series.

2. **Confirm the scrape works.** Go to http://localhost:9090/targets. Both jobs should be
   `UP`. If `demo-app` is down, the app container isn't reachable — check
   `docker compose logs app`.

3. **Run a query.** At http://localhost:9090/graph, switch to the **Graph** tab, and paste:
   ```promql
   rate(demo_http_requests_total[1m])
   ```
   You should see ~30 lines, one per label combination. Then try:
   ```promql
   sum by (status) (rate(demo_http_requests_total[1m]))
   ```
   Six lines. **This is the payoff of labels** — you changed the question without changing
   the instrumentation.

4. **Break the scrape on purpose.** Stop the app, wait 30s, and watch:
   ```promql
   up{job="demo-app"}
   ```
   go to `0`. Then `docker compose start app` and watch it return. In production, this
   metric is the first thing you alert on — if it's 0, no other metric from that target
   means anything.

5. **Watch a counter reset.** Restart the app and plot the raw counter:
   ```promql
   demo_http_requests_total
   ```
   It falls off a cliff to 0. Now plot `rate(demo_http_requests_total[1m])` across the same
   restart — it stays smooth. That's the counter rule, demonstrated.

6. **Feel the scrape interval.** Change `scrape_interval` in `prometheus/prometheus.yml` to
   `60s`, then:
   ```bash
   curl -X POST http://localhost:9090/-/reload
   ```
   Now try `rate(demo_http_requests_total[30s])` — empty, because a 30s window can't hold
   two 60s samples. Anything shorter than `2 × scrape_interval` is structurally unable to
   return data.

7. **Add your own panel.** In Grafana, Edit the dashboard → Add → Visualization, and use
   `sum(rate(demo_http_requests_total{path="/fail"}[5m]))`. Export the JSON and commit it.

8. **See cardinality hurt.** Add a label with unbounded values in `app.py`:
   ```python
   REQUESTS.labels(method="GET", path=path, status=str(status), user=str(random.randint(1, 10**6))).inc()
   ```
   Rebuild, then watch http://localhost:9090/tsdb-status. Series count climbs without
   bound. This is the failure mode; now you've seen it in a sandbox instead of on a
   production system.

---

## 8. Cheat sheet

```promql
# ── select ────────────────────────────────────────────────────────────────
up{job="demo-app"}                              # is the target alive?
demo_http_requests_total{status=~"5.."}         # regex label match

# ── rate a counter ────────────────────────────────────────────────────────
sum(rate(demo_http_requests_total[1m]))                  # total req/s
sum by (status)(rate(demo_http_requests_total[1m]))      # req/s per status
sum by (path)  (rate(demo_http_requests_total[1m]))      # req/s per endpoint

# ── percentiles from a histogram ──────────────────────────────────────────
histogram_quantile(0.95,
  sum by (le)(rate(demo_http_request_duration_seconds_bucket[5m])))

# ── error ratio ───────────────────────────────────────────────────────────
100 * sum(rate(demo_http_requests_total{status=~"5.."}[5m]))
    / clamp_min(sum(rate(demo_http_requests_total[5m])), 0.001)

# ── gauges: just read them ────────────────────────────────────────────────
demo_queue_depth
avg_over_time(demo_queue_depth[10m])            # smoothed

# ── extrapolate ───────────────────────────────────────────────────────────
predict_linear(demo_queue_depth[10m], 3600)     # value in 1h if trend holds
```

Useful endpoints:

| Endpoint | Purpose |
|---|---|
| `:9090/targets` | scrape status per target |
| `:9090/graph` | ad-hoc queries |
| `:9090/tsdb-status` | series count, storage head |
| `:9090/config` | the config Prometheus actually loaded |
| `curl -X POST :9090/-/reload` | hot-reload `prometheus.yml` |
| `:9090/api/v1/query?query=up` | the HTTP API Grafana uses |

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `demo-app` target is `DOWN` | app not up yet, or wrong hostname | `docker compose logs app`; confirm `targets:` matches the compose service name |
| Panel says "No data" | window shorter than 2× scrape interval, or no traffic | widen `[5m]` → `[10m]`; check `docker compose logs loadgen` |
| Panel blank where you expect 0 | division by zero → `NaN` | wrap the denominator in `clamp_min(x, 0.001)` |
| Grafana: "Data source with UID ... not found" | datasource provisioned without an explicit `uid` | set `uid: prometheus` to match the dashboards |
| Grafana: "Data source connected, but no data" | datasource `url` points at `localhost` | use the compose service name: `http://prometheus:9090` |
| Dashboard edits vanish | provisioning rescanned and overwrote them | set `allowUiUpdates: false` and edit the JSON, or export before restarting |
| Everything is slow / Grafana times out | cardinality explosion | check `:9090/tsdb-status`; look for high-cardinality labels |
| Old data after `down` | volumes persist by design | `docker compose down -v` to wipe |

Ports already in use? Edit the left-hand side of the mappings in `docker-compose.yml`
(e.g. `"9091:9090"`).

---

## 10. Where to go next

- **Alerting.** The point of all this is to be told when something breaks. Add
  `rule_files:` to `prometheus.yml` with a rule on `up == 0` or the error ratio above —
  it's a small file and the highest-value thing on this list.
- **`node_exporter`.** Scrape your host's CPU/memory/disk. Add one service to
  `docker-compose.yml` and one `scrape_configs` entry; the shape is identical to `app`.
- **Recording rules.** Pre-compute expensive queries so dashboards stay fast.
- **Retention and the TSDB.** `--storage.tsdb.retention.time`, plus remote_write to
  long-term storage when local disk isn't enough.
- **The official docs**, which are genuinely good:
  [prometheus.io/docs](https://prometheus.io/docs/introduction/overview/) ·
  [PromQL basics](https://prometheus.io/docs/prometheus/latest/querying/basics/) ·
  [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/).
- **Later, `docker compose down -v` and rebuild from scratch.** If it comes up correctly
  from an empty state, you genuinely understand it — that property is the reason the
  provisioning files exist at all.
