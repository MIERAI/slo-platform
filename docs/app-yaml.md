# app.yaml reference

One `app.yaml` per application (`apps/<app>/app.yaml` in your instance repository — `<instance.apps>/<app>/app.yaml`
when platform.yaml names another directory, [configuration](configuration.md#an-existing-repository);
`examples/<app>/app.yaml` in this repository) is the one place an application is registered. The platform derives everything else from it (the
ApplicationSet of the root chart, [platform/root/files/applicationset.yaml](../platform/root/files/applicationset.yaml),
mirrored by [scripts/render.py](../scripts/render.py)):

| From | Argo CD Application | Source |
|---|---|---|
| each `components[]` × environment | `<component>-<env>` | the application's own manifests |
| `slos` × environment | `<name>-slo-<env>` | chart [platform/slo](../platform/slo): Pyrra objectives, probes, the experiment runner's rights |
| `traffic` × environment (unless `trafficScale: 0`) | `<name>-traffic-<env>` | chart [platform/traffic](../platform/traffic): synthetic users |

Every cluster gets these Applications, unless platform.yaml leaves the application out of it
([`clusters.<name>.applications.exclude`](configuration.md#applications-per-cluster)).

Every Application of environment `<env>` deploys into the namespace **`<name>-<env>`**, which Argo CD creates with

- labels `slo-platform/app: <name>`, `slo-platform/env: <env>` (the optional admission policies select on them), and
  `slo-platform/image-signatures: required` when `signedImages: true`;
- the annotation `chaos-mesh.org/inject: enabled`: Chaos Mesh only injects faults into annotated namespaces;
- your own labels and annotations: platform.yaml `applications.namespaceLabels` / `namespaceAnnotations`, with this
  file's `namespace` over them ([below](#namespace)).

```yaml
name: shop                       # namespaces shop-dev, shop-prod, …
signedImages: true               # optional: label for an image-signature policy (later)
private: true                    # optional: images need pull credentials; instances without them may leave it out
namespace:                       # optional: metadata of every namespace of this application (below)
  labels: { example.com/image-signatures: required }
environments:
  - { name: dev, trafficScale: 0.3 }
  - name: prod
    promotion: { from: dev, soak: 10m }     # promotions (scripts) copy images from dev after a soak time
components:                      # how it is deployed: one Application <component>-<env> each
  - { name: shop, kustomize: deploy }       # deploy/<env>/kustomization.yaml
  - { name: shop-worker, helm: worker }     # worker/chart + worker/values-<env>.yaml
services:                        # released and promoted on their own
  - { name: web, image: ghcr.io/your-org/shop-web, component: shop }
ignoreDifferences:               # optional, passed to every Application (with RespectIgnoreDifferences)
  - { group: "", kind: Service, jqPathExpressions: ['.spec.selector["rollouts-pod-template-hash"]'] }
slos: [ … ]                      # below
traffic: { … }                   # below
```

Component names must be unique and must not end in `-slo` or `-traffic` together with the application's name
(`shop-slo`, `shop-traffic` are taken).

## namespace

`namespace.labels` / `namespace.annotations`: added to every namespace `<name>-<env>` of the application, merged over
platform.yaml's `applications.namespaceLabels` / `namespaceAnnotations` (a key in both: this file's value). The
platform's own always win and cannot be set here (`check` rejects them): `slo-platform/app`, `slo-platform/env`,
`slo-platform/image-signatures`, `chaos-mesh.org/inject`. Values are strings (quote `"true"`); label values follow
Kubernetes' rules (at most 63 characters of letters, digits, `-`, `_`, `.`).

```yaml
signedImages: true
namespace:
  labels: { example.com/image-signatures: required }   # what your own signature policy selects on
  annotations: { example.com/owner: team-a }
```

`signedImages: true` itself only adds `slo-platform/image-signatures: required` (for an image-signature policy the
product may ship later); a policy of your own that selects on another label gets that label here.

## slos

Each SLO is rendered once per environment (`environments: [prod]` limits it), as a Pyrra `ServiceLevelObjective` in
that environment's namespace: recording rules and multi-window burn-rate alerts. Every query gets
`namespace="<app>-<env>"`, so one declaration serves every environment.

Common fields: `name`, `objective` (percent), `description`, `window` (default `28d`), `runbook` (anchor in the
platform runbook; default `error-budget-burn`, `latency` for latency SLOs), `severities` (merged over fast / medium
burn = page, slow burn and absent SLI = ticket), `environments`. Exactly one SLI type:

| Type | Fields | good / valid |
|---|---|---|
| `availability` | `metric`, `matchers`, `errors` | not `metric{errors}` among `metric` (a counter) |
| `latency` | `metric`, `matchers`, `threshold` | `metric_bucket{le=threshold}` among `metric_count` (a histogram) |
| `probe` | `service`, `port`, `path` — or `url`; optional `prober`, `module`, `interval` | `probe_success == 1` among the probes (blackbox exporter every 15s, from outside the pods) |
| `age` | `metric`, `matchers`, `max` (`90s`, `26h`, `7d`) | share of time the newest good result is at most `max` old |
| `ratio` | `errors: {metric, matchers}`, `total: {metric, matchers}` | not `errors` among `total` (two counters) |
| `bool` | `metric`, `matchers` | a 0/1 gauge made elsewhere (1 = good), e.g. `cnpg:wal_archive_keeps_up:bool` |

### Metric conventions the SLIs assume

- **The `namespace` label is the application's namespace.** Scrape with a `PodMonitor` / `ServiceMonitor` in the
  application's namespace (Prometheus picks up every one in the cluster) and do not let the application set a
  `namespace` label of its own.
- **`availability`**: one request counter (e.g. `http_requests_total`) with a label that tells a server error apart:
  `errors: 'code=~"5.."'` or `errors: 'result!="ok"'`. **Client errors (4xx) are valid, not bad**: the user made the
  mistake, not the service. Select one route or class of requests with `matchers` (`app="web", route="GET /"`), and
  keep route labels low-cardinality (the route template, not the URL).
- **`latency`**: a Prometheus histogram (`<metric>_bucket`, `<metric>_count`) whose buckets include the threshold
  exactly: `threshold: "0.25"` needs a bucket `le="0.25"`. Count every request, also failed ones.
- **`age`**: a gauge holding the Unix time (seconds) of the last good run or the processing watermark, e.g.
  `last_success_timestamp_seconds`. A metric that never appeared (a job that never succeeded) is "SLI absent": a ticket.
- **`ratio`**: both counters exist from the start (export them at 0): an absent error counter is "SLI absent", not
  zero errors.
- **`probe`**: the URL answers 2xx within 5s (`http_2xx`). `service` / `port` / `path` resolve to
  `http://<service>.<namespace>.svc:<port><path>`; `url` probes anything else, and `prober: host:port` uses another
  blackbox exporter (probing from another machine, as users outside the cluster see it).

## traffic

Synthetic users, rendered per environment into a k6 Deployment `traffic` in the application's namespace: an open-loop
arrival rate (a slow service does not slow the users down), shaped like a day (quiet night, lunch and evening peaks,
bursts of 3× the peak at 12:00 and 20:00). They give the SLOs traffic in every environment and the fault experiments
something to break.

```yaml
traffic:
  peakRpm: 300                       # requests per minute at the day's peak, in an environment with scale 1
  requests:                          # weighted mix
    - { weight: 65, method: GET, service: web, port: 8080, path: "/" }
    - { weight: 7, method: POST, service: web, port: 8080, path: /orders, body: '{"amount": 100}',
        badBody: '{"amount": 0}', badRate: 0.05 }           # a share of users sends a request the API rejects (4xx)
    - { weight: 30, method: GET, service: hello, port: 8080, path: "/hello?name=user{rand}" }   # {rand} → 0..999
environments:
  - { name: dev, trafficScale: 0.3 }   # default 1; 0 = no synthetic traffic in that environment
```

- `service` / `port` / `path` resolve to `http://<service>.<app>-<env>.svc:<port><path>`: a Service of the same
  environment.
- `body` is sent as JSON (`Content-Type: application/json`); `badBody` replaces it for a share `badRate` of the
  requests (default 0.05). `{rand}` in `path` or a body becomes a random number 0..999 on every request.
- The day's shape comes from `platform.yaml`: a day of `traffic.dayMinutes` (default 1440, a real day aligned to the
  clock) in `timezone` (its standard offset: summer time is ignored here).

## Fault experiments and the fault switch

The experiment runner (ServiceAccount `experiment-runner` in `slo-experiments`) gets a Role in every namespace of an
application with SLOs (rendered by platform/slo): Chaos Mesh `PodChaos` / `NetworkChaos`, `Jobs` (an experiment's
load), and the ConfigMap **`<app>-faults`** — the convention for an application's own fault switch (an application
that reads e.g. `products-errors: "1"` from it can be made to fail on purpose). Experiments live next to the
application, in `examples/<app>/experiments/` (an instance: `apps/<app>/experiments/`, or the directories of
platform.yaml `regression.experiments`), and run on lab clusters only.
