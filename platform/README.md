# platform/

The platform's components. Rule: **nothing here names an application**. One chart, [root](root), turns an instance's
`platform.yaml` into everything a cluster runs: it renders, for the cluster named by its `cluster` parameter, one
Argo CD Application per component (from this repository at `product.version`), the ApplicationSet that turns every
`apps/<app>/app.yaml` of the instance repository into Applications ([root/files/applicationset.yaml](root/files/applicationset.yaml):
namespaces and their labels, SLOs, synthetic traffic, the experiment runner's rights in each namespace), and the root
Application itself.

## The root Application

Bootstrap creates it once; from then on the chart renders the same object, so changing `product.version` in
`platform.yaml` retargets the whole platform, the root included (upgrade = one commit in the instance repository):

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: { name: slo-platform, namespace: argocd }
spec:
  project: default
  sources:
    - repoURL: <product.repo>
      targetRevision: <product.version>
      path: platform/root
      helm:
        valueFiles: [$instance/platform.yaml]
        parameters: [{ name: cluster, value: <cluster name> }]
    - { repoURL: <instance.repo>, targetRevision: <instance.revision>, ref: instance }
  destination: { server: https://kubernetes.default.svc, namespace: argocd }
  syncPolicy: { automated: { prune: true, selfHeal: true } }
```

Its values are `platform.yaml` ([root/values.schema.json](root/values.schema.json): unknown keys are errors, defaults
documented there) plus `cluster`; rendering fails when `cluster` is missing or not in `clusters`.

## What a cluster gets

| Component | Application | Where | What |
|---|---|---|---|
| monitoring | `platform-monitoring` | every cluster | kube-prometheus-stack (`kps-*`): Prometheus (30 days on a 10 Gi volume), Alertmanager with the default routing (prod) or none (lab), Grafana |
| pyrra | `platform-pyrra` | every cluster | Pyrra operator + UI: `ServiceLevelObjective` → recording rules and burn-rate alerts; platform SLI rules |
| blackbox | `platform-blackbox` | every cluster | blackbox exporter: the prober of probe SLOs |
| [slo](slo) | per app × env (`<app>-slo-<env>`) | every cluster | chart: app.yaml `slos` → Pyrra objectives, Probes, the experiment runner's Role / RoleBinding |
| [traffic](traffic) | per app × env (`<app>-traffic-<env>`) | every cluster | chart: app.yaml `traffic` → k6 synthetic users (open loop, day-shaped: `traffic.dayMinutes`, `timezone`) |
| chaos-mesh | `platform-chaos-mesh` | every cluster | fault injection, only into namespaces annotated `chaos-mesh.org/inject=enabled`; containerd socket by `runtime` |
| cert-manager | `platform-cert-manager` | every cluster | certificates for Chaos Mesh's webhook and the CloudNativePG plugin |
| argo-rollouts | `platform-argo-rollouts` | every cluster | canary releases with analysis |
| cnpg | `platform-cnpg` | every cluster | CloudNativePG operator + Barman Cloud plugin (applications with databases) |
| [experiments](experiments) | `platform-experiments` | `role: lab` | daily fault regression (CronJob at `regression.schedule` in `timezone`), results exporter, tickets |
| kyverno + [policies](policies) | `platform-kyverno`, `platform-policies` | `optional.policies: true` | admission baseline in every application namespace (label `slo-platform/app`): pinned images, resources, readiness probe, non-root. `scripts/validate.sh` checks every application against it on each pull request, installed or not. |
| [object-storage](object-storage) | `platform-object-storage` | `objectStorage: in-cluster` | in-cluster S3 (SeaweedFS, bucket `cnpg`) for database backups. Needs the Secret `seaweedfs-s3` (key `s3.json`, [SeaweedFS S3 identities](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)) in `object-storage`. |

## Per cluster, from platform.yaml

- **role**: `prod` notifies (the routing below) and never runs experiments. `lab` gets `platform-experiments` and
  notifies nobody: Alertmanager routes everything to the receiver `null` (alerts stay visible in Alertmanager and
  Grafana; they are what the experiments check).
- **runtime**: Chaos Mesh's containerd socket — `k3s` `/run/k3s/containerd/containerd.sock`, `kind` and `generic`
  `/run/containerd/containerd.sock`. With the wrong socket every injection fails.
- **objectStorage**: `in-cluster` installs SeaweedFS (above); `none` installs nothing.
- **timezone** (IANA name): the regression CronJob's `timeZone` (summer time included), and the synthetic users' day
  (`TZ_OFFSET_HOURS` of the traffic chart). k6 gets a fixed offset: the chart takes the zone's **standard** offset from
  [root/files/timezones.yaml](root/files/timezones.yaml), so in zones with summer time the traffic's day runs one
  hour late during summer. Nothing pages on traffic volume, so the SLOs do not notice.
- **Grafana admin**: from the Secret `grafana.adminSecret` (default `grafana-admin`) in `monitoring`, keys
  `admin-user` and `admin-password`, created by bootstrap (random password unless the secrets file sets one). There is
  no default password. Grafana is only reachable through `kubectl -n monitoring port-forward svc/kps-grafana 3000:80`.
- **Storage**: Prometheus asks for 10 Gi of the default StorageClass (`retentionSize: 8GB`).
- **Lab guard**: the regression runner refuses to start unless every node carries the label `slo-platform/lab=true`
  (bootstrap labels kind nodes of a lab cluster; on another cluster: `kubectl label nodes --all slo-platform/lab=true`).

## Alert routing (Alertmanager, prod)

The default policy ([concepts](../docs/concepts.md)): only SLO alerts (`severity` page / ticket) notify, only from prod
(namespaces ending in `-dev` / `-staging` stay visible but silent), grouped by `[alertname, namespace]`. While an SLO's
fast burn pages, its medium-burn page and its tickets are inhibited. The receiver `default` is a no-op placeholder
until notification receivers arrive (P3).

## The daily regression (lab)

The CronJob `slo-experiments/experiment-regression` clones the product at `product.version` (the scripts) and the
instance at `instance.revision` (the experiments) when it starts, and runs
`scripts/experiment-regression.py --record slo-experiments/experiment-results <instance>/apps/*/experiments`. Private
repositories: the optional Secret `slo-experiments/git-credentials` (key `token`, sent as `x-access-token`), created by
bootstrap when a git token is given.

## Experiment runner rights

ServiceAccount `experiment-runner` in `slo-experiments` ([experiments/templates/rbac.yaml](experiments/templates/rbac.yaml)): cluster-wide
only read namespaces and nodes and patch namespaces (its lock and cooldown annotations); Prometheus through the API
server's service proxy; its results ConfigMap. Rights inside an application namespace come from the Role and
RoleBinding `experiment-runner` that the slo chart renders into every namespace of an application with SLOs
([slo/templates/experiment-runner.yaml](slo/templates/experiment-runner.yaml)): Chaos Mesh PodChaos / NetworkChaos,
Jobs, and the ConfigMap `<app>-faults`. Where the runner is not installed, the binding names a ServiceAccount that does
not exist and grants nothing.

## Validation

`scripts/validate.sh` renders everything as Argo CD does and runs kubeconform: the charts with their `ci/` values (so
they are tested with no application present), the root chart for every cluster of [root/ci/](root/ci) (`helm lint`
against the schema), and an instance assembled from `examples/` through [scripts/render.py](../scripts/render.py)
(root chart → Applications → their manifests, the ApplicationSet expanded). Then `k6 inspect` on every rendered traffic
configuration, the policies, and the unit tests (the root chart's behaviour: `scripts/test_root_chart.py`).
`python3 scripts/render.py --instance <dir> --cluster <name> list` / `render <name>` does the same for a real instance.
Tools: `scripts/install-tools.sh` (CI), or helm, kubectl, kubeconform, kyverno, k6 and PyYAML locally.
