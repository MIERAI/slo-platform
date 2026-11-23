# Configuration

Your installation of slo-platform is an **instance repository**: your configuration and your applications, nothing
else. The platform itself comes from the product repository (this one) **at a version** — a git tag or branch named in
your `platform.yaml`. Upgrading means changing that version.

`bin/slo-platform` (Python 3 with PyYAML) creates, checks and upgrades an instance repository:

```sh
slo-platform init <dir> --owner <gh-owner> --repo <name> [--cluster name:role:runtime[:objectStorage] ...]
                  [--example <app> ...] [--version <ref>] [--timezone <IANA>] [--product-repo <url>]
                  [--age [--age-key-file <path>]]
slo-platform check [<dir>]
slo-platform upgrade --version <ref> [<dir>]
```

## The instance repository

```
platform.yaml                      # the one configuration file (below)
apps/<app>/app.yaml                # an application (docs/app-yaml.md)
apps/<app>/...                     # its manifests: component paths are relative to apps/<app>/
apps/<app>/experiments/*.yaml      # its fault experiments (docs/experiments.md)
secrets/<cluster>.sops.yaml        # optional, SOPS-encrypted (below)
.sops.yaml                         # who can decrypt the secrets (age recipients)
README.md                          # next steps
.gitignore                         # OpenTofu state (.tofu/, *.tfstate*), decrypted files
```

`apps/` is the default: `instance.apps` names another directory, or `.` for applications at the repository root, and
`regression.experiments` other experiment directories — for a repository that existed before slo-platform
([An existing repository](#an-existing-repository)).

Argo CD reads `platform.yaml` and `apps/*/app.yaml` from the instance repository at `instance.revision`; the platform's
charts and components come from `product.repo` at `product.version`. An application's own manifests (its components)
come from the instance repository; its SLOs and synthetic users are rendered by the product's `platform/slo` and
`platform/traffic` charts at the product's version.

### `init`

| Option | Default | |
|---|---|---|
| `<dir>` | — | created; an existing directory must be empty |
| `--owner`, `--repo` | — | the GitHub repository the instance will be pushed to: `instance.repo` = `https://github.com/<owner>/<repo>.git` |
| `--cluster` | `lab:lab:kind` | repeatable: `name:role:runtime[:objectStorage]` (`lab:kind` / `prod:k3s` when the name is the role) |
| `--example` | `sample-app` | repeatable: copies `examples/<app>/` of this product (with its experiments, without results) to `apps/<app>/`; `none` for an empty `apps/` |
| `--version` | `main` | `product.version` |
| `--timezone` | `Etc/UTC` | `timezone` |
| `--product-repo` | this repository | `product.repo` (a fork) |
| `--age` | off | SOPS + age: see [Secrets](#secrets) |
| `--age-key-file` | `$SOPS_AGE_KEY_FILE`, else `~/.config/sops/age/keys.txt` | the age key, outside the repository; an existing key is used, never overwritten |

`init` checks the result (as `check` does) before it returns. Then follow the instance's README: push it, bootstrap a
cluster (`bootstrap/` of this repository, see [bootstrap/README.md](../bootstrap/README.md)).

### `check`

Exits non-zero with one line per problem:

- `platform.yaml` against `platform/root/values.schema.json` of the product checkout the command runs from — the file
  Helm validates the root chart's values with (the chart's `values.yaml` defaults merged in, as Helm does) — and the
  time zone against the IANA database.
- `clusters.<name>.extras`: names unique per cluster, every `path` a directory of the instance;
  `clusters.<name>.applications.exclude`: applications of this instance, each once;
  `clusters.<name>.environments.exclude` / `applications.environments.<app>.exclude`: environments of the
  applications (of that application), each once, none of an application excluded there, and every application the
  cluster runs keeps at least one environment ([Environments per cluster](#environments-per-cluster));
  `components.<name>`:
  a known component, and `values` only for the Helm-sourced ones ([Components](#components)); namespace labels and
  annotations (`applications.*`, app.yaml `namespace`): valid keys, string values (labels: valid label values), none of
  the platform's own (`slo-platform/*`, `chaos-mesh.org/inject`).
- every `<instance.apps>/*/app.yaml` (default `apps/*/app.yaml`): `name` is the directory name (a DNS label); `environments` is a non-empty list of unique
  names, `trafficScale` ≥ 0, `promotion.from` an earlier environment; `components` unique, each exactly one of
  `kustomize` / `helm`, not `<app>-slo` / `<app>-traffic`, and their files exist for every environment
  (`<dir>/<env>/kustomization.yaml`; `<dir>/chart/Chart.yaml` and `<dir>/values-<env>.yaml`); `services` name a
  component; every SLO has a unique name, an objective between 0 and 100, **exactly one SLI type** with its required
  fields, and `environments` among the application's; `traffic` has `peakRpm` > 0 and requests with `weight` > 0,
  `method`, `service`, `port`, a `path` starting with `/`, `badRate` between 0 and 1.
- every `*.yaml` of the experiment directories (`regression.experiments`, default `<instance.apps>/*/experiments`): a
  unique name across applications; `namespace` is `<app>-<env>` of an existing
  application and environment; `fault` is a list, durations parse; every expected alert (`fire`, `quiet`, `may_fire`)
  with an `slo` label names a declared SLO (`<app>-<slo name>`) of the application the namespace belongs to and an
  environment the SLO is rendered in. An experiment whose SLO was renamed or removed fails here, not in the lab at 9:00.

Warnings (`!`, exit code unchanged): a prod cluster whose `notify` receivers need a secret key that
`secrets/<cluster>.sops.yaml` lacks, or that has no secrets file ([notify](#how-the-secrets-get-there)).

### `upgrade`

Sets `product.version` by editing that one line: comments and formatting stay. Check, commit, push; each cluster's root
Application reads the new version from `platform.yaml` and retargets itself and every platform component.

## platform.yaml

`apiVersion: slo-platform/v1`. The same file is the values of the root chart (`platform/root`), which Argo CD renders
once per cluster with `cluster: <name>` added. An annotated example: [platform.example.yaml](../platform.example.yaml).

| Key | Default | Meaning |
|---|---|---|
| `apiVersion` | — (required) | `slo-platform/v1` |
| `product.repo` | — (required) | Git URL of the product repository (this one, or a fork) |
| `product.version` | — (required) | Git ref (tag or branch) of the product every cluster runs. Upgrade = change it |
| `instance.repo` | — (required) | Git URL of the instance repository (this file, `apps/`) |
| `instance.revision` | `main` | Git ref of the instance repository Argo CD follows |
| `instance.apps` | `apps` | Directory of the applications, `<apps>/<app>/app.yaml`; `.` = the repository root. Relative, no `..` |
| `timezone` | `Etc/UTC` | IANA name: the daily regression's CronJob `timeZone`, the synthetic users' day (below) |
| `language` | `en` | `en`, `zh`, `ja`: notification texts (`notify.language` overrides it) |
| `clusters.<name>.role` | — (required) | `prod`: notifies, never runs experiments. `lab`: fault experiments and the daily regression; notifies nobody |
| `clusters.<name>.runtime` | `generic` | `kind`, `k3s`, `generic`: Chaos Mesh's container runtime socket (`/run/k3s/containerd/containerd.sock` on k3s, `/run/containerd/containerd.sock` otherwise) |
| `clusters.<name>.objectStorage` | `none` | `in-cluster`: an S3-compatible store (SeaweedFS) for database backups |
| `clusters.<name>.extras` | `[]` | Your own components on that cluster: `[{ name, path }]`, [below](#extras) |
| `clusters.<name>.applications.exclude` | `[]` | Applications (directory names under `instance.apps`) this cluster does not run, [below](#applications-per-cluster) |
| `clusters.<name>.environments.exclude` | `[]` | Environments (app.yaml `environments[].name`) this cluster does not run, of any application, [below](#environments-per-cluster) |
| `clusters.<name>.applications.environments` | `{}` | Per application: `{ <app>: { exclude: [<env>] } }`, that application's environments this cluster does not run, [below](#environments-per-cluster) |
| `clusters.<name>.components` | `{}` | Same keys as `components`, for that cluster only, deep-merged over the top-level ones — e.g. a small lab: `{ monitoring: { values: { grafana: { enabled: false } } } }` |
| `optional.policies` | `false` | Kyverno and the workload baseline (admission policies in application namespaces) |
| `applications.namespaceLabels` | `{}` | Labels of every application namespace `<app>-<env>` (app.yaml `namespace.labels` is merged over them) |
| `applications.namespaceAnnotations` | `{}` | Annotations of every application namespace (app.yaml `namespace.annotations` is merged over them) |
| `components.<component>.enabled` | `true` | `false`: the platform component is not installed ([Components](#components)) |
| `components.<component>.values` | — | Helm-sourced components only: deep-merged into its Helm values |
| `components.tracing.enabled` | `false` | Opt-in: Grafana gets a Tempo datasource for `components.tracing.external` ([Tracing](#tracing)) |
| `components.tracing.external.queryUrl` | — | An external Tempo's HTTP API as Grafana reaches it (`http://tempo.example:3200`); required when enabled |
| `components.tracing.external.endpoint` | — | Its OTLP/HTTP receiver (`http://tempo.example:4318`): recorded for people and tools, not passed into applications |
| `components.logging.enabled` | `false` | Opt-in: Grafana gets a Loki datasource for `components.logging.external` ([Logs](#logs)) |
| `components.logging.external.queryUrl` | — | An external Loki's HTTP API as Grafana reaches it (`http://loki.example:3100`); required when enabled |
| `components.logging.external.endpoint` | — | Its push API (`http://loki.example:3100/loki/api/v1/push`): recorded for your log collector, the platform installs none |
| `grafana.adminSecret` | `grafana-admin` | Secret in `monitoring` with keys `admin-user`, `admin-password`; bootstrap creates it |
| `regression.schedule` | `0 9 * * *` | Cron schedule of the daily experiment regression on lab clusters, in `timezone` |
| `regression.experiments` | `<instance.apps>/*/experiments` | Experiment directories (a glob or a list of globs, relative to the instance root): the daily regression runs their `*.yaml`, `check` checks them |
| `traffic.dayMinutes` | `1440` | Length of the synthetic users' day in minutes (1..1440); less compresses it (60: a day per hour, for tests) |
| `notify` | `{}` | Who hears what on prod clusters: [below](#notify) |

Cluster names are DNS labels (lower case, digits, `-`; at most 40 characters). Unknown keys are errors (typos fail the
check, not silently the platform).

**Time zones and summer time.** The CronJob takes the IANA name as it is (summer time included). The synthetic users
(k6) get a fixed offset from UTC: the zone's **standard** offset. In a zone with summer time, their day is shifted by
an hour in summer (the lunch peak at 13:00 local instead of 12:00).

**Roles.** Faults never touch a cluster that is not `lab`: the daily regression (`platform-experiments`) is rendered
only for lab clusters, and it additionally refuses to run unless every node carries the label
`slo-platform/lab=true` (bootstrap labels kind nodes; on other lab clusters label them yourself:
`kubectl label nodes --all slo-platform/lab=true`).

**Namespace metadata.** Argo CD creates each application namespace `<app>-<env>` with the labels and annotations of
`applications.namespaceLabels` / `namespaceAnnotations`, then app.yaml's `namespace.labels` / `namespace.annotations`
over them ([app.yaml](app-yaml.md#namespace)), then the platform's own over both: `slo-platform/app`,
`slo-platform/env`, `slo-platform/image-signatures` (app.yaml `signedImages: true`) and `chaos-mesh.org/inject:
enabled`. `check` rejects those keys in your maps. Use them for admission policies or pull-secret replication of your
own that select namespaces by label.

### extras

`clusters.<name>.extras` brings your own components to one cluster without forking the platform: each entry
`{ name, path }` becomes an Argo CD Application **`extras-<name>`** in `argocd`, whose source is the instance repository
at `instance.revision`, path `<path>`: a kustomize directory (with a `kustomization.yaml`) or a directory of plain
manifests (not recursive). Typically the directory holds Argo CD Applications (your UIs, backups, extra probes), which
then manage their own destinations. Synced automatically with prune and self-heal: removing a file there removes what it
made.

```yaml
clusters:
  home:
    role: prod
    extras:
      - { name: home, path: clusters/home }     # clusters/home/kustomization.yaml → Applications of your own
```

Names: DNS labels, unique per cluster (at most 46 characters). `path`: relative, no `..`; `check` requires it to exist.

### Applications per cluster

Every cluster runs every application of the instance, unless its `clusters.<name>.applications.exclude` names it (the
application's directory name). The ApplicationSet then makes no Applications of that application on that cluster:
its git generator gets one more `files` entry, `{ path: <apps>/<app>/app.yaml, exclude: true }`; without the key
(or with an empty list) the ApplicationSet is exactly as before. Use it when clusters differ in what they run: a small
lab that keeps one application out to make room for another, or an application only prod has.

```yaml
clusters:
  lab:
    role: lab
    applications: { exclude: [storefront] }   # every application but storefront
```

Excluding an application that already runs there removes its Applications, not its workloads (the ApplicationSet
preserves resources on deletion): delete the namespaces `<app>-<env>` yourself afterwards. Fault experiments that
target an excluded application's namespaces fail on that cluster: move or skip them. `check` requires each name to
be an application of the instance.

### Environments per cluster

Every cluster runs every environment of each application it runs, unless platform.yaml leaves some out — for every
application (`clusters.<name>.environments.exclude`) or for one (`clusters.<name>.applications.environments.<app>
.exclude`, `<app>` its directory name). The two add up. The ApplicationSet then makes no Applications
(`<component>-<env>`, `<name>-slo-<env>`, `<name>-traffic-<env>`) for those environments on that cluster: each
environment of its environments list is wrapped in one more `if`; without the keys (or with empty lists) the
ApplicationSet is exactly as before. Use it when a cluster needs only part of an application: a small lab that keeps
dev for its checks but not the staging and prod copies prod already runs, or a cluster that runs prod only.

```yaml
clusters:
  lab:
    role: lab
    environments: { exclude: [staging] }        # no application's staging on the lab
    applications:
      environments:
        catalog: { exclude: [prod] }             # catalog: dev only (its staging is gone already)
```

This is platform.yaml's, not app.yaml's: an application describes its environments once, clusters choose among them
(as with [`applications.exclude`](#applications-per-cluster)). Promotions are not affected: they move image tags
between the environments' files in the instance repository (app.yaml `promotion.from`, soak time from the git
history), whichever clusters deploy them — an environment a cluster leaves out still exists for the others, and for
promotions. A cluster that excludes the `promotion.from` environment of one it keeps simply receives versions that
ran elsewhere.

Excluding an environment that already runs there removes its Applications, not its workloads (the ApplicationSet
preserves resources on deletion): delete the namespace `<app>-<env>` yourself afterwards. Fault experiments that
target it fail on that cluster: move or skip them. `check` requires every name to be an environment of some
application (`environments.exclude`) or of that application (`applications.environments`), each once; an application
excluded there takes no environment settings, and an application the cluster runs must keep at least one environment
(to leave all out, use `applications.exclude`).

## Components

The platform components the root chart installs, each an Argo CD Application `platform-<component>`:

| `components.<…>` | Source | Installed |
|---|---|---|
| `monitoring` | Helm: kube-prometheus-stack | every cluster |
| `blackbox` | Helm: prometheus-blackbox-exporter | every cluster |
| `chaos-mesh` | Helm: chaos-mesh | every cluster |
| `experiments` | Helm: this product's `platform/experiments` | lab clusters |
| `pyrra` | kustomize | every cluster |
| `cert-manager` | kustomize | every cluster |
| `argo-rollouts` | kustomize | every cluster |
| `cnpg` | kustomize | every cluster |
| `kyverno`, `policies` | kustomize | with `optional.policies: true` |
| `object-storage` | kustomize | clusters with `objectStorage: in-cluster` |
| `tracing` | none: a Grafana datasource for an external Tempo | only with `enabled: true` (opt-in): [Tracing](#tracing) |
| `logging` | none: a Grafana datasource for an external Loki | only with `enabled: true` (opt-in): [Logs](#logs) |

- `enabled: false` drops the component (its Application; Argo CD prunes what it installed). `enabled: true` is the
  default and does not override the condition in the last column. What depends on a dropped component is yours to
  provide: Pyrra and the SLOs need the monitoring stack's CRDs, cnpg and chaos-mesh need cert-manager, the canary
  examples need Argo Rollouts.
- `values` (Helm-sourced components only) are **deep-merged** into the component's Helm values as the platform sets
  them (the templates of `platform/root/templates/platform-<component>.yaml`): maps merge key by key; lists, strings,
  numbers, booleans and `null` replace what is there (`false` is a value, not "unset"; `null` removes the key, as in
  any Helm values file). The kustomize-sourced components take no `values`: `check` (and Helm) reject them; drop the
  component and bring your own through [extras](#extras) instead.

```yaml
components:
  monitoring:
    values:
      prometheus:
        prometheusSpec:
          retentionSize: 12GB
          storageSpec:     # maps merge: the platform's accessModes stay, only the size changes
            volumeClaimTemplate:
              spec: { resources: { requests: { storage: 15Gi } } }
      grafana:
        additionalDataSources:
          - { name: Prometheus · lab, uid: lab-prometheus, type: prometheus, access: proxy, url: "http://lab.example:30087" }
  cnpg: { enabled: false }          # no databases here
```

Merging over the monitoring stack's Alertmanager `config` replaces parts of the platform's routing ([notify](#notify)
builds it): prefer `notify`.

### Tracing

`tracing` is opt-in (`enabled` defaults to `false`) and, in this release, **external only**: you run Grafana Tempo
somewhere outside the cluster — a machine that observes production without being in its request path — and the
platform gives the cluster's Grafana (platform-monitoring) a Tempo datasource (name `Tempo`, uid `tempo`) for it. No
Application, no Tempo in the cluster; the rest of the render is unchanged.

```yaml
components:
  tracing:
    external:
      queryUrl: http://tempo.example:3200   # Tempo's HTTP API: the datasource's URL (required when enabled)
      endpoint: http://tempo.example:4318   # its OTLP/HTTP receiver: what your services export to
clusters:
  prod:
    role: prod
    components: { tracing: { enabled: true } }   # on for this cluster only (or components.tracing.enabled for all)
```

- The datasource is a provisioning file of its own (`grafana.datasources."tempo.yaml"` of platform-monitoring), so
  your own `grafana.additionalDataSources` (a list, which replaces on merge) do not drop it, and it does not drop them.
  Turning it on restarts Grafana once (the chart's checksum of its datasources).
- **The applications decide whether they send traces**, per environment: the platform passes nothing into them. With
  an OpenTelemetry SDK that is `OTEL_EXPORTER_OTLP_ENDPOINT=<external.endpoint>` in that environment's manifests,
  typically dev first and the other environments through your normal promotion.
- Tempo unreachable: the SDKs' batch span processors export in the background and drop spans when their queue is full
  (2048 spans by default) — requests are not slowed or failed; you see fewer traces.
- `check` refuses `enabled: true` without `external.queryUrl` (top level or on a cluster), and URLs that are not
  `http(s)://`.

### Logs

`logging` works like [Tracing](#tracing), for Grafana Loki: opt-in, external only — the cluster's Grafana gets a Loki
datasource (name `Loki`, uid `loki`, provisioning file `loki.yaml` next to `tempo.yaml`). The platform installs no
Loki and no collector: ship the logs with your own (e.g. Grafana Alloy through [extras](#extras)) to
`external.endpoint`, and give it a bounded queue so that an unreachable Loki costs log lines, never the workloads.

```yaml
components:
  logging:
    external:
      queryUrl: http://loki.example:3100                      # the datasource's URL (required when enabled)
      endpoint: http://loki.example:3100/loki/api/v1/push     # where your collector pushes
clusters:
  prod: { role: prod, components: { logging: { enabled: true } } }
```

## An existing repository

A GitOps repository that already holds applications (one directory per application with an `app.yaml`, experiments,
Argo CD Applications of your own per cluster) becomes an instance without moving anything: add a `platform.yaml` at its
root that says where things are.

```yaml
apiVersion: slo-platform/v1
product: { repo: https://github.com/MIERAI/slo-platform.git, version: v0.2.0 }
instance: { repo: https://github.com/your-org/gitops-manifests.git, revision: main, apps: "." }   # <app>/app.yaml at the root
clusters:
  home:
    role: prod
    runtime: k3s
    extras: [{ name: home, path: clusters/home }]   # your own Applications on this cluster
  lab:
    role: lab
    runtime: k3s
    extras: [{ name: lab, path: clusters/lab }]
applications:
  namespaceLabels: { example.com/policies: enforced }   # what your own admission policies select on
components:
  monitoring: { values: { prometheus: { prometheusSpec: { retentionSize: 12GB } } } }
regression:
  experiments: experiments          # experiments/*.yaml instead of <app>/experiments/
```

Then:

1. `slo-platform check .` from a checkout of the product: every `app.yaml` found, every experiment's namespace and SLOs
   resolved, every extras path present.
2. Move what the platform now installs out of your own tree: the Argo CD Applications for its components (monitoring,
   Pyrra, Chaos Mesh, …) and the ApplicationSet that turned `app.yaml` files into Applications. Keep what is yours in
   the `extras` directories (they must no longer include those). A component you customise beyond `values` (a
   different kustomize path, patches): `enabled: false`, and its Application of yours in `extras`.
3. Per-application namespace labels (e.g. one that requires signed images only where `signedImages: true`) go into
   that `app.yaml` as `namespace.labels` ([app.yaml](app-yaml.md#namespace)).
4. Bootstrap each cluster (`bootstrap/`): its root Application renders `platform/root` with this file.

`scripts/render.py --instance . --cluster <name> all <dir>` renders what Argo CD will render, to compare with what the
cluster runs before switching.

## notify

Who hears what. Only clusters with `role: prod` notify; a lab cluster notifies nobody, whatever `notify` says (its
alerts are what the experiments check: they stay visible in Alertmanager and Grafana). On prod, the platform's routing
([concepts](concepts.md), [platform/README.md](../platform/README.md#alert-routing-alertmanager-prod)) stays as it is:
only SLO alerts notify (`severity` `page` or `ticket`), only from namespaces not ending in `-dev` / `-staging`, grouped
by alert name and namespace, a fast burn's page inhibiting the same SLO's medium-burn page and tickets. `notify` only
says where each severity goes:

```yaml
notify:
  page:                                  # receivers of every page (each gets every notification)
    - { type: telegram, chatId: "-1001234567890" }
    - { type: ntfy, topic: my-team-pages-7f3a9c }
  ticket:                                # receivers of every ticket
    - { type: email, to: ops@example.com, from: alertmanager@example.com, smarthost: "smtp.example.com:587", username: alertmanager }
  deadman: { url: https://hc-ping.com/<uuid>, interval: 1m }   # optional
  language: en                           # optional: overrides `language` for these texts
```

| Key | Default | Meaning |
|---|---|---|
| `notify.page`, `notify.ticket` | none | Receivers (below). An empty or missing list: that severity notifies nobody |
| `notify.deadman.url` | — | Dead man's switch: Alertmanager's `Watchdog` alert (always firing) is POSTed there every `interval`; the watcher (e.g. [healthchecks.io](https://healthchecks.io)) alarms when the pings stop — when Prometheus, Alertmanager or the cluster is gone. Leave `url` out to read it from the secret `deadmanUrl` (or `secretKey`) |
| `notify.deadman.interval` | `1m` | Time between pings. Give the watcher this period and some grace |
| `notify.language` | `language` | `en`, `zh`, `ja` |

With `notify: {}` (the default) nothing notifies, as before.

**Texts.** Every receiver gets the same short text in the chosen language: a title
`[PAGE] <namespace>: <SLO>` (`[TICKET]`; `[PAGE RESOLVED]` when it is over; the alert name instead of the SLO when a
notification groups several SLOs), then per alert the SLO, its burn-rate window, the summary and the runbook link (the
SLO's `pyrra.dev/summary` and `pyrra.dev/runbook`, set by the `slo` chart). Resolved notifications are sent too.
Templates: [platform/root/files/notify/](../platform/root/files/notify) (one file per language holds the words).

### Receivers

Alertmanager's own integrations (Alertmanager v0.34, the version kube-prometheus-stack runs here). Every receiver has a
`type`; unknown keys are errors. Secrets never go into `platform.yaml`: each type reads its secret from a key of the
secrets file's `notify:` (below), named by default as in the table; `secretKey` names another key (e.g. two Slack
channels with two webhooks).

| `type` | Fields | Secret key (default) | Example |
|---|---|---|---|
| `telegram` | `chatId` (required: a number, `-100…` for groups and channels), `threadId` (forum topic) | `telegramBotToken` (from @BotFather) | `{ type: telegram, chatId: "-1001234567890" }` |
| `slack` | `channel` (overrides the webhook's) | `slackWebhookUrl` (an incoming webhook URL) | `{ type: slack, channel: "#ops" }` |
| `discord` | — | `discordWebhookUrl` | `{ type: discord }` |
| `email` | `to` (comma-separated), `from`, `smarthost` (`host:port`), `username`, `requireTls` (default `true`: STARTTLS) | `smtpPassword`, only with `username` | `{ type: email, to: ops@example.com, from: am@example.com, smarthost: "smtp.example.com:587", username: am }` |
| `ntfy` | `topic` (required), `url` (default `https://ntfy.sh`) | none; `secretKey` for an access token (protected topics) | `{ type: ntfy, topic: my-team-pages-7f3a9c }` |
| `pushover` | — | `pushoverToken` (application), `pushoverUserKey` (user or group; `userSecretKey` renames it) | `{ type: pushover }` |
| `msteams` | — | `msteamsWebhookUrl` (a Teams Workflows webhook) | `{ type: msteams }` |
| `mattermost` | `channel` | `mattermostWebhookUrl` | `{ type: mattermost, channel: ops }` |
| `webhook` | `url` (when it holds no secret) | `webhookUrl`, when `url` is left out | `{ type: webhook, url: "http://bridge.tools.svc:8080/alerts" }` |

- **ntfy** goes through Alertmanager's webhook with a custom payload (ntfy's JSON publishing to the server's root URL):
  title and text as above, priority 5 for a firing page (3 when resolved), 3 for a ticket (2), a tag. Alertmanager
  has no native ntfy integration. On a public server a topic is readable by anyone who knows its name: use a long
  random name, or a protected topic with an access token (`secretKey: ntfyToken`).
- **pushover**: priority 1 (high) for a firing page, 0 otherwise.
- **webhook** sends Alertmanager's own JSON (version 4), for programs (a ticket system, a chat bridge).
- Other integrations of Alertmanager (PagerDuty, Opsgenie, …) are not wired in yet: a `webhook` to a bridge, or a
  pull request adding the type to `platform/root/templates/_notify.tpl` and the schema.

### How the secrets get there

```
secrets/<cluster>.sops.yaml  notify: { telegramBotToken: … }
  → bootstrap (tofu apply)   Secret monitoring/alertmanager-notify, one key each (always created, empty without any)
  → platform-monitoring      alertmanagerSpec.secrets: [alertmanager-notify], only when a receiver reads a secret
  → Alertmanager             /etc/alertmanager/secrets/alertmanager-notify/<key>, read through bot_token_file,
                             api_url_file, webhook_url_file, url_file, auth_password_file, token_file, user_key_file,
                             credentials_file at each notification
```

A key the Secret lacks fails only that receiver, at its first notification (Alertmanager logs it). `slo-platform check`
warns about it: it reads the key names of `secrets/<cluster>.sops.yaml` (SOPS leaves names readable; no key needed)
for every prod cluster. After changing the secrets file, run bootstrap again (`tofu apply`) to update the Secret;
Alertmanager picks the files up on its own. An installation bootstrapped before notifications existed lacks the Secret:
run bootstrap once before adding a receiver that needs one.

## Secrets

`secrets/<cluster>.sops.yaml`, one per cluster, encrypted with [SOPS](https://github.com/getsops/sops) for the age
recipients in `.sops.yaml`. Everything in it is optional: bootstrap generates throwaway values for what is missing,
which is fine for a lab.

```yaml
git:
  token: ghp_...          # read access for Argo CD to private product / instance repositories (and the regression's clone)
grafana:
  adminPassword: ...      # → Secret monitoring/<grafana.adminSecret>; random when absent
notify:                   # → Secret monitoring/alertmanager-notify: the receivers' secrets (see notify above)
  telegramBotToken: ...
  smtpPassword: ...
secrets:                  # for applications: namespace → Secret name → key: value
  storefront-prod:
    payment-api: { api-key: ... }
```

`init --age`:

1. uses the age key at `--age-key-file` if it exists, else creates one with `age-keygen` (mode 0600). The key never
   goes into the repository; **back it up**: without it the secrets cannot be decrypted.
2. writes `.sops.yaml` with its public key as the recipient of `secrets/*.sops.yaml`;
3. writes `secrets/<cluster>.sops.yaml` per cluster, encrypted, with the keys above as comments. Without `sops` it
   writes an unencrypted `secrets/example.yaml` instead (gitignored) and warns: encrypt it with
   `sops -e secrets/example.yaml > secrets/<cluster>.sops.yaml`, then delete it.

Edit a secrets file with `sops secrets/<cluster>.sops.yaml` (decrypts into `$EDITOR`, encrypts on save). Add a
teammate: append their age public key to `age:` in `.sops.yaml` (comma-separated), then `sops updatekeys
secrets/*.sops.yaml`.

**OpenTofu state holds secrets.** Bootstrap keeps its state in the instance directory (`.tofu/<cluster>.tfstate`); it
contains the Argo CD repository credentials, Grafana's password and the notification secrets in plain text. `init`'s `.gitignore` excludes it:
never commit it, and keep the directory as private as the age key.
