# PromQL cookbook

The queries you actually reach for, roughly in the order you reach for them.
Generic Prometheus metric names are used throughout so this stays useful beyond
this repo — for the exact equivalents in *this* project's demo service, see
[§15 Mapping to this repo](#15-mapping-to-this-repo).

---

## 0. Before you start: run and read

Type these into **http://localhost:9090/graph** (Graph and Table tabs), or hit the
API — the same one Grafana uses:

```bash
curl -s 'http://localhost:9090/api/v1/query'    --data-urlencode 'query=up'
curl -s 'http://localhost:9090/api/v1/query_range' --data-urlencode 'query=rate(http_requests_total[5m])' \
     --data-urlencode 'start=2026-01-01T00:00:00Z' --data-urlencode 'end=2026-01-01T01:00:00Z' \
     --data-urlencode 'step=15s'
```

Two shapes, and mixing them up causes most syntax errors:

| Shape | Looks like | Is | Used by |
|---|---|---|---|
| **Instant vector** | `http_requests_total` | one sample per series, *now* | comparisons, aggregation, everything |
| **Range vector** | `http_requests_total[5m]` | all samples in a window | `rate`, `increase`, `*_over_time` — **cannot be graphed directly** |

---

## 1. Selecting series

```promql
http_requests_total                                   # exact name
http_requests_total{job="api"}                        # one label
http_requests_total{job="api", method="POST"}         # two labels ANDed
http_requests_total{code=~"5.."}                      # regex match
http_requests_total{code!~"2..|3.."}                  # negative regex
http_requests_total{code!="200"}                      # not equal
http_requests_total{handler=~"/api/.*"}               # prefix-ish
{__name__=~"http_.*"}                                 # name is just a label
{job="api"}                                           # legal, but scans everything — avoid
http_requests_total{env=""}                           # label exists but is empty
http_requests_total{env=~".+"}                        # label exists and is non-empty
```

`=~` is **fully anchored** — `code=~"5.."` means exactly five-then-anything-two-chars,
not "contains 5". Use `.*5.*` if you want substring matching.

---

## 2. Counters: rate, increase, and friends

Never graph a raw counter. `http_requests_total = 41872` only ever grows.

```promql
rate(http_requests_total[5m])          # per-second average rate  <- the workhorse
increase(http_requests_total[1h])      # total increase over the window
irate(http_requests_total[5m])         # only the last 2 samples — spiky, fast-reacting
delta(cpu_temp_celsius[5m])            # difference over window, for GAUGES
idelta(cpu_temp_celsius[5m])           # last two samples only
deriv(cpu_temp_celsius[5m])            # per-second slope via linear regression
resets(http_requests_total[1h])        # how many times it restarted (should be 0)
changes(config_last_reload_successful[1h])  # how often the value changed
```

**`rate` vs `irate`:** `rate` averages over the whole window (smooth, use for
dashboards and alerts). `irate` uses only the final two samples (twitchy, use when
you need to see a change *now* — and never for alerting, it flaps).

**`rate` vs `increase`:** same math, different unit. `rate` = per second,
`increase` = total over the window. Both **extrapolate** to the window edges, so
`increase(x[1h])` is an estimate, not a counted total.

**Both need at least 2 samples in the window.** With a 15s scrape interval,
`rate(x[10s])` is structurally guaranteed to return nothing. Rule of thumb: the
window should be ≥ 4× your `scrape_interval`.

`rate` also absorbs **counter resets** for free: if the process restarted
mid-window, it detects the drop and compensates. That's the whole reason to use it
instead of subtracting two samples yourself.

---

## 3. Aggregating

```promql
sum(rate(http_requests_total[5m]))                        # one number
sum by (method) (rate(http_requests_total[5m]))           # keep method, drop the rest
sum without (instance) (rate(http_requests_total[5m]))    # drop instance, keep the rest
avg by (instance) (rate(node_cpu_seconds_total[5m]))
max by (service) (queue_depth)
min by (service) (queue_depth)
count by (job) (up)                                       # how many targets per job
stddev by (service) (request_duration_seconds)
stdvar  by (service) (request_duration_seconds)
topk(5, sum by (path) (rate(http_requests_total[5m])))   # 5 busiest
bottomk(5, sum by (path) (rate(http_requests_total[5m])))
quantile(0.9, request_duration_seconds)                   # across SERIES, not time
group by (service) (up)                                   # 1 per group, no value
```

`quantile()` here aggregates **across series at one instant** — that is *not*
latency percentiles. For those you need a histogram (§5).

**Aggregation deletes labels.** After `sum by (method) (...)`, `path` is gone
forever — you cannot filter on it downstream. Keep what you'll need to group on
before you wrap.

---

## 4. Errors, ratios and SLOs

```promql
# error ratio, as a percentage
100 * sum(rate(http_requests_total{code=~"5.."}[5m]))
    / clamp_min(sum(rate(http_requests_total[5m])), 0.001)

# success ratio
sum(rate(http_requests_total{code=~"2.."}[5m]))
  / clamp_min(sum(rate(http_requests_total[5m])), 0.001)

# availability over a 30-day window (SLO)
1 - (
  sum(increase(http_requests_total{code=~"5.."}[30d]))
  / clamp_min(sum(increase(http_requests_total[30d])), 1)
)

# errors per second, by code
sum by (code) (rate(http_requests_total{code=~"[45].."}[5m]))

# anything currently failing
http_requests_total{code=~"5.."} > 0
```

**`clamp_min` is not optional.** A bare division by zero yields `NaN`, and a `NaN`
panel renders blank — which looks exactly like an outage. Every ratio needs a
zero-case guard.

**Use the same range on both sides.** A `[5m]` numerator over a `[1h]` denominator
is a meaningless number that still renders a clean-looking graph.

---

## 5. Latency from a histogram

This is the single most valuable thing PromQL does, and the most commonly botched.

```promql
# percentiles — note `by (le)`, it is NOT optional
histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# split by a label — keep le AND the dimension you care about
histogram_quantile(0.95, sum by (le, path) (rate(http_request_duration_seconds_bucket[5m])))

# mean latency (as opposed to median — note they differ a lot under load)
sum(rate(http_request_duration_seconds_sum[5m]))
  / clamp_min(sum(rate(http_request_duration_seconds_count[5m])), 0.001)

# request rate, which is just the count
sum(rate(http_request_duration_seconds_count[5m]))

# THE APACHE/NGINX BUCKET TRICK: what fraction of requests are slower than 0.5s?
1 - (
  sum(rate(http_request_duration_seconds_bucket{le="0.5"}[5m]))
  / clamp_min(sum(rate(http_request_duration_seconds_bucket{le="+Inf"}[5m])), 0.001)
)

# audit your buckets before trusting any quantile
sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
```

Read the quantile expression as: *aggregate every bucket across all series, then
interpolate inside the bucket that contains the Nth percentile.*

Three rules:

1. **`by (le)` is mandatory.** Without it the function can't see the bucket
   structure and silently returns garbage.
2. **`le` must survive your aggregation.** `sum without (le) (...)` destroys it.
3. **If p99 ≈ your largest bucket, the number is fiction.** Its entire mass has
   landed in `+Inf`, so the value is interpolated inside the top bucket. Run the
   last query above to check before you report a percentile.

`histogram_quantile` returns an **estimate** — bucket interpolation, not an exact
measurement. That's the price of being cheaply aggregatable across instances,
which client-side summaries are not.

---

## 6. Gauges and time-window functions

```promql
queue_depth                                  # just read it — never rate() a gauge
avg_over_time(queue_depth[10m])              # smoothed
min_over_time(queue_depth[10m])
max_over_time(queue_depth[10m])
sum_over_time(queue_depth[10m])              # unusual for gauges, common for counters
count_over_time(queue_depth[10m])            # how many samples actually arrived
last_over_time(queue_depth[10m])
present_over_time(queue_depth[10m])          # 1 if any sample exists
stddev_over_time(queue_depth[10m])           # volatility

deriv(queue_depth[10m])                      # per-second slope
predict_linear(queue_depth[10m], 3600)       # where the trend puts it in 1 hour
double_exponential_smoothing(queue_depth[10m], 0.5, 0.5)   # replaces holt_winters()
```

`predict_linear` is the classic disk-fill / queue-growth query. Pair it with a
threshold to alert *before* the wall:

```promql
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 4*3600) < 0
```

---

## 7. Availability and target health

```promql
up                                            # 1 = scrape ok, 0 = scrape failed
up{job="api"}                                 # one job
up == 0                                       # -> EMPTY means healthy. Alert on this.
up == bool 0                                  # -> 1 when down, 0 when up (alerting form)
count by (job) (up)                           # targets per job
count by (job) (up == 1)                      # targets actually up
avg by (job) (up)                             # fraction up — good SLI

absent(up{job="api"})                         # 1 only if the series vanished entirely
absent_over_time(up{job="api"}[1h])           # 1 if no data for an hour

scrape_duration_seconds{job="api"}            # how long a scrape takes
scrape_samples_scraped{job="api"}             # series per scrape — watch for growth
scrape_series_added{job="api"}                # NEW series per scrape
```

**`up == 0` returning empty is the most misread result in PromQL.** Empty means
"nothing is down", not "the query is broken". This is why `up == 0` is the first
alert anyone writes: if a target's `up` is 0, *no other metric from it means
anything*, because you're looking at stale data.

`absent()` is the inverse: it fires when the series is missing entirely — a
different failure from "the series exists and is 0".

---

## 8. Saturation and capacity

```promql
# CPU busy %, per instance
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# memory used %
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)

# filesystem used %
100 * (1 -
  node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
  / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}
)

# network throughput
rate(node_network_receive_bytes_total{device!="lo"}[5m]) * 8   # bits/s
rate(node_network_transmit_bytes_total{device!="lo"}[5m])

# in-flight requests vs a concurrency limit
sum(in_flight_requests) / clamp_min(concurrency_limit, 1)

# queue saturation
queue_depth / clamp_min(queue_capacity, 1)

# container memory working set (cAdvisor / kubelet)
container_memory_working_set_bytes{container!=""}

# CPU throttling — the "my app is slow but CPU looks idle" answer
rate(container_cpu_cfs_throttled_periods_total[5m])
  / clamp_min(rate(container_cpu_cfs_periods_total[5m]), 1)
```

---

## 9. Comparing series across metrics

```promql
# filter by value — this FILTERS, it does not return 1/0
queue_depth > 100
# ...unless you add bool, which returns 1/0 per series
(queue_depth > 100) or (queue_depth <= 100)     # clunky
queue_depth > bool 100                          # -> 1 or 0

# vector matching: join two metrics on a shared label
http_requests_total / on(instance) http_requests_total offset 1d

# group_left keeps labels from the "one" side; group_right when reversed
rate(http_requests_total[5m])
  / on(instance) group_left(version) build_info

# set operators
metric_a and metric_b          # series present in BOTH
metric_a or  metric_b          # union — the workhorse for fallbacks
metric_a unless metric_b       # in A but not in B

# the empty-result guard: force a 0 instead of a blank panel
sum(rate(http_errors_total[5m])) or vector(0)
```

`on(...)` / `ignoring(...)` control which labels must match for a binary
operation. `group_left` / `group_right` are required when one side has more series
than the other.

---

## 10. Label manipulation

```promql
label_replace(up, "host", "$1", "instance", "([^:]+):.*")     # extract host from instance
label_replace(rate(x_total[5m]), "svc", "$1", "__name__", "(.+)_total")
label_join(up{job="api"}, "target", "/", "job", "instance")   # concatenate labels
sum without (instance) (rate(http_requests_total[5m]))        # drop a label
sum by (method) (rate(http_requests_total[5m]))               # keep only this label
```

`label_replace` signature: `label_replace(v, dst, replacement, src, regex)`. The
replacement can reference `$1` capture groups.

---

## 11. Time shifts

```promql
http_requests_total offset 1w              # the value one week ago
rate(x_total[5m] offset 1d)                # the rate as of yesterday

# week-over-week comparison
sum(rate(http_requests_total[1h]))
  - sum(rate(http_requests_total[1h] offset 1w))

# pin to an absolute instant (repeatable dashboards, no "now" drift)
x_total @ 1609746000
x_total @ end()                            # the query's end time
rate(x_total[5m] @ start())

# subquery: apply a range function to an aggregated expression
max_over_time( sum(rate(http_requests_total[1m]))[10m:1m] )
avg_over_time( (a / b)[30m:1m] )
```

`offset` past the start of your retention returns nothing — a very common
"why is my comparison blank" trap.

---

## 12. Cardinality and TSDB health

```promql
prometheus_tsdb_head_series                          # THE number to watch
sum(scrape_samples_scraped) by (job)                 # what each job contributes

# which metrics have the most series?
topk(10, count by (__name__) ({__name__=~".+"}))

# which label VALUES are exploding?
count by (path) ({__name__="http_requests_total"})
topk(10, count by (user_id) ({__name__=~".+"}))

# ingest rate and storage
rate(prometheus_tsdb_head_samples_appended_total[5m])
prometheus_tsdb_storage_blocks_bytes
prometheus_tsdb_retention_limit_seconds

# scrapes going wrong
prometheus_target_scrapes_exceeded_sample_limit_total
prometheus_target_scrapes_sample_out_of_order_total
prometheus_target_scrape_pool_reloads_failed_total

# is Prometheus itself healthy?
up{job="prometheus"}
prometheus_tsdb_compactions_failed_total
prometheus_rule_evaluation_failures_total
```

If `prometheus_tsdb_head_series` climbs and never falls, you have a
high-cardinality label — almost always something like `user_id`, `request_id`, a
raw URL, or a timestamp stuffed into a label. Go read [§14](#14-the-mistakes-that-bite).

---

## 13. Alerting-rule building blocks

These are the *shapes* you put in a rules file, not things you graph:

```yaml
groups:
  - name: availability
    rules:
      - alert: TargetDown
        expr: up == 0
        for: 2m                       # must stay true this long before firing
        labels: { severity: critical }
        annotations:
          summary: "{{ $labels.job }} target {{ $labels.instance }} is down"

      - alert: HighErrorRate
        expr: |
          100 * sum(rate(http_requests_total{code=~"5.."}[5m]))
              / clamp_min(sum(rate(http_requests_total[5m])), 0.001) > 5
        for: 10m

      - alert: HighLatency
        expr: |
          histogram_quantile(0.95,
            sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) > 1
        for: 10m

      - alert: DiskWillFillIn4Hours
        expr: predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 4*3600) < 0
        for: 10m

      - alert: TooManyRestarts
        expr: increase(process_start_time_seconds[15m]) > 0
```

**Always add `for:`.** Without it, a single scrape blip pages someone at 3am.

---

## 14. The mistakes that bite

| Symptom | Cause | Fix |
|---|---|---|
| Query returns nothing, looks broken | The value genuinely isn't there (`up == 0` when all is well) | Empty ≠ zero. Use `or vector(0)` if you need a 0 |
| Panel blank where you expect `0` | Division by zero → `NaN` | `clamp_min(denominator, 0.001)` |
| `rate` returns nothing | Window < 2× scrape interval | Widen the window to ≥ 4× `scrape_interval` |
| Percentiles look wrong / jump around | Missing `by (le)`, or `le` was aggregated away | `sum by (le)` — always |
| p99 equals your top bucket | Everything landed in `+Inf` | Add a bigger bucket to the instrumented histogram |
| Graph has huge spikes after a restart | You're graphing a raw counter | `rate()` it |
| `rate()` on a gauge gives nonsense | Gauges aren't monotonic | Read gauges directly, or `deriv()` |
| Comparison query is empty | `offset` is past your retention | Check `prometheus_tsdb_retention_limit_seconds` |
| Alerts flap every scrape | `irate()` in an alert | Use `rate()`, and add `for:` |
| `sum by (...)` result can't be filtered | Aggregation deleted the label | Keep the label in `by (...)` |
| Prometheus is slow / OOM | Cardinality explosion | `topk(10, count by (__name__)({__name__=~".+"}))` |
| Rate looks 15× too big after a config change | `scrape_interval` shortened, old code assumed the old one | Don't hardcode sample counts; always use `rate()` |

---

## 15. Mapping to this repo

This project's `app/app.py` exports these (verified against the running stack):

| Generic name used above | This project's metric | Labels |
|---|---|---|
| `http_requests_total` | `demo_http_requests_total` | `method`, `path`, `status` |
| `http_request_duration_seconds` | `demo_http_request_duration_seconds` | `path` |
| `in_flight_requests` | `demo_http_requests_in_progress` | — |
| `queue_depth` | `demo_queue_depth` | — |
| `http_errors_total` | `demo_jobs_processed_total` | `outcome` |
| `build_info` | `demo_build_info` | `version` |

> **Histograms are the one exception to this name mapping.** `demo_http_request_duration_seconds`
> is the *family* name — it appears in the `# TYPE` line and in the table above — but there is
> **no series by that name**. The samples on the wire are `..._bucket`, `..._count` and `..._sum`.
> Query `..._bucket` with `sum by (le)`, or `..._count` / `..._sum`. Selecting the bare family
> name returns an empty result — the trap described in [§5](#5-latency-from-a-histogram).

Endpoints: `/`  `/healthz`  `/work`  `/slow`  `/fail`  `/nope`
(so `status` takes `200`, `404`, `500` — `/fail` always 500s and `/nope` always 404s,
by design).

**Start with these five.** They cover the four golden signals and are all runnable
against `http://localhost:9090/graph` as soon as the stack is up:

```promql
# 1. Is anything down?
up == 0

# 2. Where is traffic going?
sum by (path) (rate(demo_http_requests_total[1m]))

# 3. What fraction is failing?
100 * sum(rate(demo_http_requests_total{status=~"5.."}[5m]))
    / clamp_min(sum(rate(demo_http_requests_total[5m])), 0.001)

# 4. How bad is the tail?
histogram_quantile(0.95,
  sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))

# 5. Is throughput healthy?
sum by (outcome) (rate(demo_jobs_processed_total[1m]))
```

These five are also exactly the queries behind the panels on the provisioned
**Demo App** dashboard in Grafana — open any panel and choose *Edit* to see them in
context.

Worth trying once, because it's the lesson the demo is built to teach: run the
average and the p99 side by side against `/slow`.

```promql
sum(rate(demo_http_request_duration_seconds_sum[5m]))
  / sum(rate(demo_http_request_duration_seconds_count[5m]))         # mean  ~0.18 s

histogram_quantile(0.99,
  sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m]))) # p99   ~3.6 s
```

A perfectly respectable-looking mean hiding a 20× worse tail. That gap is why you
record a `Histogram` and not an average.

---

## 16. Quick reference

```promql
# ── counters ──────────────────────────────────────────────────────────────
rate(x_total[5m])                        increase(x_total[1h])
irate(x_total[5m])                       irate = last 2 samples, don't alert on it
resets(x_total[1h])                      should be 0

# ── gauges ────────────────────────────────────────────────────────────────
x                                        avg_over_time(x[10m])
max_over_time(x[10m])                    deriv(x[10m])
predict_linear(x[6h], 4*3600)            delta(x[5m])

# ── histograms ────────────────────────────────────────────────────────────
histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))
sum(rate(x_sum[5m])) / sum(rate(x_count[5m]))     # mean
sum(rate(x_count[5m]))                            # request rate

# ── aggregation ───────────────────────────────────────────────────────────
sum by (l) (x)     avg by (l) (x)     max by (l) (x)
min by (l) (x)     count by (l) (x)   topk(5, x)
sum without (l) (x)                   quantile(0.9, x)   # across series, not time

# ── ratios (always guard the denominator) ─────────────────────────────────
100 * sum(rate(x_total{code=~"5.."}[5m])) / clamp_min(sum(rate(x_total[5m])), 0.001)

# ── availability ──────────────────────────────────────────────────────────
up == 0            absent(up{job="api"})        avg by (job) (up)

# ── set operations ────────────────────────────────────────────────────────
a and b            a or b            a unless b
sum(rate(errors[5m])) or vector(0)              # force 0 instead of blank

# ── joins ─────────────────────────────────────────────────────────────────
a / on(instance) group_left(version) build_info

# ── time ──────────────────────────────────────────────────────────────────
x offset 1w        x @ end()        max_over_time( (a/b)[30m:1m] )

# ── cardinality ───────────────────────────────────────────────────────────
prometheus_tsdb_head_series
topk(10, count by (__name__) ({__name__=~".+"}))
```

**The two rules that cover most of it:** `rate()` a counter, then `sum by (...)`
a label. Everything above is a variation on those two moves.
