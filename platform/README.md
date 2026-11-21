# platform/

The platform's components. Each one is an Argo CD Application in [apps/](../apps) (one file each); a root Application
renders `apps/` (the default set) and, if wanted, `apps/optional/`. Rule: **nothing here names an application**.
Everything per application comes from its `app.yaml` ([docs/app-yaml.md](../docs/app-yaml.md)) through
[apps/applicationset.yaml](../apps/applicationset.yaml): namespaces and their labels, SLOs, synthetic traffic, and the
experiment runner's rights in each namespace.

## Default set (`apps/`)

| Component | Application | What |
|---|---|---|
| monitoring | `platform-monitoring` | kube-prometheus-stack (`kps-*`): Prometheus (30 days on a 10 Gi volume), Alertmanager with the default routing, Grafana |
| pyrra | `platform-pyrra` | Pyrra operator + UI: `ServiceLevelObjective` → recording rules and burn-rate alerts; platform SLI rules |
| blackbox | `platform-blackbox` | blackbox exporter: the prober of probe SLOs |
| [slo](slo) | per app × env (`<app>-slo-<env>`) | chart: app.yaml `slos` → Pyrra objectives, Probes, the experiment runner's Role / RoleBinding |
| [traffic](traffic) | per app × env (`<app>-traffic-<env>`) | chart: app.yaml `traffic` → k6 synthetic users (open loop, day-shaped) |
| chaos-mesh | `platform-chaos-mesh` | fault injection, only into namespaces annotated `chaos-mesh.org/inject=enabled` |
| [experiments](experiments) | `platform-experiments` | daily fault regression (CronJob, lab clusters only), results exporter, tickets |
| cert-manager | `platform-cert-manager` | certificates for Chaos Mesh's webhook and the CloudNativePG plugin |
| argo-rollouts | `platform-argo-rollouts` | canary releases with analysis |
| cnpg | `platform-cnpg` | CloudNativePG operator + Barman Cloud plugin (applications with databases) |

## Optional (`apps/optional/`)

| Component | What | Needs |
|---|---|---|
| kyverno + [policies](policies) | admission baseline in every application namespace (label `slo-platform/app`): pinned images, resources, readiness probe, non-root. `scripts/validate.sh` checks every application against it on each pull request, installed or not. | — |
| [object-storage](object-storage) | in-cluster S3 (SeaweedFS, bucket `cnpg`) for database backups | Secret `seaweedfs-s3` (key `s3.json`, [SeaweedFS S3 identities](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)) in `object-storage` |

## Alert routing (Alertmanager)

The default policy ([concepts](../docs/concepts.md)): only SLO alerts (`severity` page / ticket) notify, only from prod
(namespaces ending in `-dev` / `-staging` stay visible but silent), grouped by `[alertname, namespace]`. While an SLO's
fast burn pages, its medium-burn page and its tickets are inhibited. The receiver `default` is a no-op placeholder
until notification receivers arrive (P3).

## Settings per cluster (until `platform.yaml`, P2)

Patch the Application (e.g. a kustomize overlay over `apps/`):

- **k3s**: Chaos Mesh needs k3s's containerd socket — `platform-chaos-mesh`
  `spec.source.helm.valuesObject.chaosDaemon.socketPath: /run/k3s/containerd/containerd.sock` (the default
  `/run/containerd/containerd.sock` fits kind and most distributions). With the wrong socket every injection fails.
- **Grafana admin**: the default password is the well-known `prom-operator` (a random one would change on every
  render); Grafana is only reachable through `kubectl -n monitoring port-forward svc/kps-grafana 3000:80`. Before
  exposing it, create a Secret and set `grafana.admin: { existingSecret: <name>, userKey: admin-user, passwordKey:
  admin-password }`.
- **Storage**: Prometheus asks for 10 Gi of the default StorageClass (`retentionSize: 8GB`); raise both together.
- **Experiments**: install `platform-experiments` on lab clusters only. The runner refuses to start unless every node
  carries the label `slo-platform/lab=true`; on other clusters the job fails at once and the regression tickets
  (stale / broken) would fire.

## Experiment runner rights

ServiceAccount `experiment-runner` in `slo-experiments` ([experiments/rbac.yaml](experiments/rbac.yaml)): cluster-wide
only read namespaces and nodes and patch namespaces (its lock and cooldown annotations); Prometheus through the API
server's service proxy; its results ConfigMap. Rights inside an application namespace come from the Role and
RoleBinding `experiment-runner` that the slo chart renders into every namespace of an application with SLOs
([slo/templates/experiment-runner.yaml](slo/templates/experiment-runner.yaml)): Chaos Mesh PodChaos / NetworkChaos,
Jobs, and the ConfigMap `<app>-faults`. Where the runner is not installed, the binding names a ServiceAccount that does
not exist and grants nothing.

## Validation

`scripts/validate.sh` renders everything as Argo CD does and runs kubeconform, the charts' `ci/` values (so the charts
are tested with no application present), `k6 inspect` on every rendered traffic configuration, and the policies.
Tools: `scripts/install-tools.sh` (CI), or helm, kubectl, kubeconform, kyverno, k6 and PyYAML locally.
