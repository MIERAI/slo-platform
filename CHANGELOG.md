# Changelog

All notable changes; the format follows [Keep a Changelog](https://keepachangelog.com/), versions follow SemVer.

## [Unreleased]

## [0.2.0] - 2026-11-21

### Changed
- `init` pins the newest release tag of the product checkout by default (was `main`).

### Fixed (found by a clean-room reproduction: a new user following the docs in another GitHub organisation)
- Quickstart: PyYAML on macOS (PEP 668: virtualenv or uv), the Grafana service name, committing experiment results,
  gh with SSH protocol.
- Bootstrap on an existing cluster: keep its kubeconfig in its own file (k3d example; `k3d cluster delete` rewrites
  ~/.kube/config unless KUBECONFIG points elsewhere), choosing runtime k3s, the `next` output carries --kubeconfig,
  removing the platform's workloads before destroy.
- sample-app-down's hypothesis names the probe SLO (its prediction always did).

### Notes
- P5: the reference installation (two k3s clusters, prod and lab) runs v0.1.1 from an existing GitOps repository
  (`instance.apps: "."`, extras, per-cluster components); switched in ~40 s per cluster with no resource recreated.

### Added
- `clusters.<name>.components`: per-cluster component settings, deep-merged over `components` (clusters differ).
- Generated fault experiments (P6): `scripts/generate-experiments.py` (and `slo-platform experiments generate|check`)
  writes `<app>/experiments/generated/<app>-<slo>-<env>.yaml` per SLO from `experiments/templates/<SLI type>.yaml`,
  when the pods to fail can be derived from app.yaml and the manifests (else: skipped, with the reason). probe: every
  pod behind the Service fails → fast-burn page within a bound from the objective; availability, latency, age, ratio,
  bool: the SLI's pods fail → `SLOMetricAbsent` (a generic fault cannot make an in-pod SLI count bad events). Never
  overwrites without `--force` nor a file without `generated:`; `--missing` for uncovered SLOs only; `--check` is the
  coverage gate (`scripts/validate.sh` runs it on the examples). The examples gain the four experiments their
  hand-written ones did not cover (sample-app hello-availability / hello-latency, storefront web-probe / api-latency).
- `experiment.py` checks an experiment's format before it runs (`check_format`: unknown fields refused, `generated:`
  allowed); the regression, scorecard and alert report read `experiments/generated/` too; reports of generated
  experiments go to `experiments/results/`.

## [0.1.0] - 2026-11-21

### Added
- An existing GitOps repository as an instance (P5), all optional with today's behaviour as the default:
  `instance.apps` (where `<app>/app.yaml` directories live; `.` = the repository root), `regression.experiments`
  (experiment directories of the daily regression and `check`), `clusters.<name>.extras` (Applications `extras-<name>`
  from the instance repository: your own components per cluster), `components.<component>` (`enabled: false` drops a
  platform component; `values` deep-merged into the Helm-sourced ones: maps merge, lists replace),
  `applications.namespaceLabels` / `namespaceAnnotations` and app.yaml `namespace: { labels, annotations }` (the
  platform's own labels and the chaos annotation always win). `check`, `scripts/render.py` (plain-manifest
  directories too) and `scripts/appdesc.py` follow them; CI renders `platform/root/ci/existing-repo.yaml` and the
  examples laid out that way.
- `docs/quickstart.md` (P4): from nothing to the first passing experiment on kind, with troubleshooting;
  `docs/runbook.md`: what to do per alert (ErrorBudgetBurn per SLI type, SLOMetricAbsent, the regression's tickets).
- `scripts/e2e.sh`: the template instantiated end to end on a throwaway kind cluster without GitHub (an in-cluster git
  server serves this checkout and an `init` instance; `bootstrap/tofu/kind`; every Application Synced + Healthy;
  `--experiment`: sample-app-down must PASS; diagnosis on failure). Workflow `e2e`: weekly, on `v*` tags (with the
  experiment), on pull requests that change it, on demand.
- Notifications (P3): `notify` in platform.yaml routes pages and tickets of prod clusters to receivers of Alertmanager's
  own integrations — Telegram, Slack, Discord, e-mail, ntfy (webhook with ntfy's JSON publishing), Pushover, Teams,
  Mattermost, webhook — plus a dead man's switch (`Watchdog` to an outside watcher). Texts in en / zh / ja. Secrets from
  the secrets file's `notify:` → Secret `monitoring/alertmanager-notify` (bootstrap) → Alertmanager's `*_file` fields.
  `check` warns about missing secret keys. CI: `amtool check-config` on every rendering, the configuration through
  kube-prometheus-stack, and a delivery test against local stand-ins (`scripts/notify-delivery.sh`).
- The instance model (P2): your repository holds `platform.yaml` (apiVersion slo-platform/v1) and `apps/`; the platform
  comes from this repository at `product.version`. Root chart `platform/root` (values = platform.yaml + `cluster`;
  roles prod / lab; self-managed root Application; time zones with their standard offset), replacing `apps/`.
- `bin/slo-platform`: `init` (instance repository, examples, optional age/SOPS; `.tofu/` and `.secrets/` owner-only),
  `check` (platform.yaml schema, app.yaml, experiments reference declared SLOs), `upgrade --version`.
- `bootstrap/tofu`: module (Argo CD, repository credentials, Grafana admin, generic Secrets, root Application) and the
  roots `kind` (labels lab nodes) and `cluster` (existing cluster, explicit context). `docs/configuration.md`.

### Changed
- Grafana's admin login comes from a Secret (no fixed default password); the daily regression only on lab clusters,
  cloning the product at its version and the instance's experiments.
- Platform components (P1): monitoring with default alert routing (only prod notifies, inhibition), Pyrra, the `slo`
  chart (6 SLI types, probe url/prober, per-namespace experiment-runner Role), blackbox, Chaos Mesh, `traffic` chart
  (k6 per app × environment from `app.yaml` `traffic`, real day aligned to the clock), daily experiment regression,
  cert-manager, Argo Rollouts, CloudNativePG; optional: Kyverno workload baseline, in-cluster object storage.
- ApplicationSet: every `examples/*/app.yaml` → Applications; namespaces labelled `slo-platform/app`, `slo-platform/env`.
- Examples: `sample-app` (canary releases), `storefront` (dependency chain, asynchronous pipeline, 8 SLOs).
- Experiment tooling: `experiment.py` (`--prometheus`, `--context`, runs on the same day append instead of
  overwriting), `experiment-regression.py` (node label `slo-platform/lab=true` instead of a cluster name),
  `scorecard.py`, `alert-report.py`; 14 example experiments; `scripts/test.sh`, `scripts/validate.sh`.
- Docs: `docs/app-yaml.md`, `docs/experiments.md`.
- Repository layout, license (Apache-2.0), contribution and security policies, CI skeleton, `platform.example.yaml`
  (configuration draft).
