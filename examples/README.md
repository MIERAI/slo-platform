# examples/

Two applications, each a directory with its deploy manifests and an `app.yaml` (format: [docs/concepts.md](../docs/concepts.md)
and the comments in [storefront/app.yaml](storefront/app.yaml)). Their images are public (`ghcr.io/mierai/sample-app`,
`ghcr.io/mierai/storefront-{web,api,worker}`): no pull secret and no image signatures are needed.

| | sample-app (quickstart) | storefront (reference) |
|---|---|---|
| Shape | one HTTP service | web → api, web → Postgres ← worker |
| Environments | dev → prod | dev → staging → prod |
| Deployed as | kustomize (`overlays/<env>`), an Argo Rollouts canary | kustomize (`overlays/<env>`: web, api, Postgres) + Helm (`worker/chart`, `values-<env>.yaml`) |
| SLI types | availability, latency, probe | availability, latency, probe, age (freshness), ratio (completeness) |
| Installed by default | yes | no |

## sample-app — the quickstart

- **SLOs**: `/hello` availability (5xx), latency (250ms bucket edge), and a blackbox probe that sees what the in-pod
  metrics cannot (no pod left, a broken Service).
- **Canary releases with automatic abort** (`base/rollout.yaml`): 50% → 30s → analysis → 100%. The analysis judges the
  canary pods on their real request metrics (error ratio, p95, from Prometheus: `base/analysis.yaml`) and runs a k6
  load test against the canary pods only (`base/load-test.yaml`). Any failure aborts and the stable version keeps serving.
- **`base/traffic.yaml`** (a curl loop, 2 req/s, every environment) stays on purpose: the platform's synthetic users
  follow a day profile, and at night they send sample-app a few requests per minute — too few for the analysis, which
  fails safe (aborts) when no request reached the canary in the last minute. Remove it only if your traffic never drops
  below about 1 req/s.

## storefront — the reference

- **Dependency chain**: web renders the page from api's products. web's SLOs are what users see and page; api's own
  SLOs tell whether a web symptom is api's fault. A page without products is a 200 counted as `degraded`, an error for
  `web-availability`.
- **Asynchronous pipeline**: web stores orders in Postgres (CloudNativePG), the worker processes them. Three SLIs:
  `order-intake` (POST /orders without a 5xx), `order-freshness` (age of the oldest unprocessed order below 60s) and
  `order-completeness` (orders processed within 5 minutes). They are measured by web asking the database, not by the
  worker: a worker's metrics vanish with the last worker.
- **Fault switches**: api reads `/etc/faults` from the optional ConfigMap `storefront-faults`; an experiment creates it
  (e.g. `products-errors: "1"`) and deletes it to end the fault.
- Requires the CloudNativePG operator (the platform installs it).

## Synthetic users

Both `app.yaml`s declare `traffic`: the platform runs a k6 Deployment per application and environment that follows a
day (quiet nights, a lunch and an evening peak, two bursts of 3× the peak) and makes mistakes (stale links, bad
orders: 4xx, valid events, not errors). `peakRpm` is the peak in an environment with `trafficScale: 1` (prod); dev and
staging get 0.3 of it. storefront 210 + sample-app 90 = 300 rpm at prod's peak.

## Reaching them

Every Service is `ClusterIP`. Use port-forward:

```sh
kubectl -n sample-app-dev port-forward svc/sample-app 8080:8080   # http://localhost:8080/hello?name=you
kubectl -n storefront-prod port-forward svc/web 8081:8080         # http://localhost:8081/
```

To expose one permanently, patch its Service in the overlay (`type: NodePort` or `LoadBalancer`) or add an Ingress.

## Simplified compared with the reference installation

| Removed | Why | To re-enable |
|---|---|---|
| NodePort Services | the ports only made sense on one machine's network | patch the Service in `overlays/<env>` (see above) |
| `imagePullSecrets` | the images are public | add `imagePullSecrets: [{ name: <secret> }]` to the pod templates for private images |
| `signedImages: true` | the signature policy is optional in the platform | build and sign your images with your own key, then set `signedImages: true` |
| External probes (from another machine to prod) | they name that machine's addresses | an SLO with `probe: { url: ..., prober: ... }` in your own `app.yaml` |
| Postgres backups | the object store is optional; the orders are synthetic | add a CloudNativePG backup (barman-cloud plugin or `spec.backup.barmanObjectStore`) with a credentials Secret and a `ScheduledBackup` to `storefront/base/postgres.yaml` |
