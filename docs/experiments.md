# Fault experiments

Alerts are tested like code. An experiment injects one fault and states, **before** it runs, which alerts must fire
within how long, which must stay quiet, and that nothing else may page. `scripts/experiment.py` runs it and writes a
report; `scripts/experiment-regression.py` runs every experiment daily on a lab cluster. A wrong prediction is a
finding: write a new experiment (`-v2`), never edit the predictions of one that ran ([concepts](concepts.md), 3).

Each application keeps its experiments next to its `app.yaml`: `examples/<app>/experiments/<name>.yaml`. The file
name is the experiment's name; names are unique across applications (results and metrics are keyed by it).

## Format

```yaml
hypothesis: >-                     # what breaks, and the prediction with its reasoning, written before the run
  Every sample-app pod fails. The in-pod SLI cannot see it (no pod left to count); the probe SLO must page.
namespace: sample-app-dev          # <app>-<env>; Chaos Mesh only acts in namespaces annotated chaos-mesh.org/inject=enabled
fault:                             # created in the namespace, always deleted at the end
  - apiVersion: chaos-mesh.org/v1alpha1
    kind: PodChaos
    metadata: { name: sample-app-all-down }
    spec:
      action: pod-failure
      mode: all
      selector: { labelSelectors: { app: sample-app } }
      duration: 30m                # >= the experiment's time: the fault ends by itself if the runner dies
effect: { query: 'max(probe_success{namespace="sample-app-dev"}) == 0', within: 3m }   # proof that the fault bites
timeout: 15m                       # how long the fault stays at most
hold: 0s                           # keep the fault this long even when every expected alert fired (negative tests)
expect:
  fire:                            # each must fire within its time
    - { alert: ErrorBudgetBurn, labels: { namespace: sample-app-dev, slo: sample-app-hello-probe, severity: page, long: 1h }, within: 10m }
  quiet: []                        # must not page; name `severity` in the labels to hold a ticket quiet too
  may_fire: []                     # allowed, not required (logged "~ also fired")
recover: 15m                       # the fired alerts resolve within this after the fault
```

| Field | Meaning |
|---|---|
| `hypothesis` | What is broken, what users see, and the prediction (which alert, after how long, and why). |
| `namespace` | Where the fault and the load are created. The runner locks it (one experiment per namespace). |
| `fault` | Chaos Mesh objects with `spec.duration` ≥ the experiment's time (`timeout` and every `within`), or a fault-switch ConfigMap the application reads (the runner adds `expires`: start + that time + 5 min, which the application must honour). Anything else is refused: it would not end by itself if the runner died. An empty list is a negative test. |
| `load` | Optional Kubernetes objects the fault needs to matter (e.g. a Job that writes), created before the fault and deleted with it. A Job needs `activeDeadlineSeconds`. |
| `effect` | A PromQL query that returns data once the fault bites, within `within` (default 3m). Chaos Mesh's `AllInjected` only says the rules are in place. Required for a fault that is not a Chaos Mesh object. |
| `timeout`, `hold` | The fault stays until every `expect.fire` fired and `hold` passed, at most `timeout` (or the largest `within`). |
| `expect.fire` | `alert`, `labels`, `within` (default 10m). Must fire in time; it must not already fire before the fault. |
| `expect.quiet` | Must not fire. Without `severity` in its labels an entry means "must not page": a lab namespace with a fault every day keeps its 1d / 4d slow-burn tickets firing however well the SLO works. |
| `expect.may_fire` | Allowed, neither required nor unexpected. |
| `recover` | After the fault, the fired alerts resolve within this (`--no-recover` skips the check). |
| `ignore` | Namespaces (a regex) whose pages notify nobody on this cluster: not judged. |
| `skip` | Why the regression does not run it (e.g. `superseded by <name>-v2 (...)`). Kept for the record: the scorecard ignores skipped experiments. |

The SLO alerts come from Pyrra: `ErrorBudgetBurn` with labels `namespace`, `slo` (`<app>-<slo name>`), `severity`
(`page` / `ticket`) and the windows `long` / `short` (fast burn: `long: 1h`, 14× the budget rate, `for: 2m`), and
`SLOMetricAbsent` (ticket). Every page that fires during the run and is not in `fire` / `may_fire` fails the
experiment — in its own namespace, and in any namespace no other experiment holds.

**NetworkChaos and Services.** A `target` matches the target pods' IPs. A client that reaches a database through its
Service sends packets to the Service IP (translated on the node, outside the pod), so a fault selected on the client
with the database pods as target never matches. Select the database and target the clients: the replies carry the
clients' pod IPs (see `examples/storefront/experiments/storefront-worker-slow.yaml`).

## Running one

On a lab cluster only (see [Safety](#safety)):

```sh
pip install pyyaml        # or: uv run --with pyyaml python3 scripts/experiment.py ...
python3 scripts/experiment.py --context my-lab examples/sample-app/experiments/sample-app-down.yaml
# several at once, one per namespace (they share the 1h burn-rate windows):
python3 scripts/experiment.py examples/storefront/experiments/api-errors.yaml \
  examples/storefront/experiments/storefront-worker-down.yaml
```

| Flag | Default | |
|---|---|---|
| `--kubeconfig`, `--context` | kubectl's (current context) | the context in use is printed at the start and written into the report |
| `--prometheus` | `monitoring/kps-prometheus:9090` | `<namespace>/<service>:<port>`, reached through the API server's service proxy |
| `--results DIR` | `results/` next to each experiment | where the reports go |
| `--record NS/CONFIGMAP` | — | also store each result in a ConfigMap (the regression's, for the metrics below) |
| `--no-recover` | — | skip the `recover` check |

The log goes to the terminal and to `<results>/<date>-<name>.md` (the date the run started). A second run of the
same experiment on the same day **appends** a section (`## Run <start>: <STATUS>`, with the kubectl context); the
first run's findings are never overwritten. Exit code: 0 all passed, 1 some failed, 2 none failed but some were
invalid.

**PASS / FAIL / INVALID.** A run whose time did not flow is INVALID, neither pass nor fail: the wall clock jumped
more than 1 min between two polls (machine asleep, VM paused), or Prometheus has a hole of more than 2 min in the run
or in the hour before it (the 1h burn-rate windows; also right after a Prometheus restart). It says nothing about the
alerts. On macOS the runner keeps the machine awake while it runs (`caffeinate`); a closed lid still sleeps.

## The scheduled regression

Every experiment, every day, on the lab cluster: `platform/experiments` (a CronJob) clones the repository and runs

```sh
python3 scripts/experiment-regression.py --record slo-experiments/experiment-results
```

| Flag | Default | |
|---|---|---|
| `DIR ...` | `examples/*/experiments` | experiment directories, globs allowed |
| `--lab-label` | `slo-platform/lab=true` | every node must carry it, or nothing starts |
| `--kubeconfig`, `--context`, `--prometheus` | as above | in the CronJob: the pod's service account |
| `--gap` | `45m` | a namespace stays marked this long after each experiment |
| `--wait` | `2h` | an experiment whose namespace is not free by then is skipped this time |
| `--only a,b` | all | these experiments only |
| `--dry-run` | — | print the plan and stop |

One queue per namespace, the queues in parallel. Within a namespace the negative tests come first, then the faults,
each one only when the namespace is free: no experiment of anyone running, its cooldown over, nothing paging there,
none of its expected alerts already firing, no Prometheus gap in the last hour. Not run, and said so: `skip:`, a
namespace that does not exist on this cluster. A full run of the storefront takes hours (45 min between faults).

```sh
python3 scripts/experiment-regression.py --context my-lab --dry-run                     # the plan
kubectl -n slo-experiments create job --from=cronjob/experiment-regression regression-$(date +%s)   # a run now
kubectl -n slo-experiments get configmap experiment-results -o jsonpath='{.data.api-down\.json}'     # last result
```

Results become metrics (`experiment-exporter`, from the ConfigMap; manual runs with `--record` update it too).
Labels `experiment`, `namespace`, and per expectation `kind` (fire / quiet), `alert`, `slo`, `severity`, `long`:
they join to the SLOs on `namespace` + `slo`.

| Metric | |
|---|---|
| `slo_experiment_last_success` | 1: the last valid run passed (invalid runs do not change it) |
| `slo_experiment_last_status{status}` | the last run, valid or not: pass / fail / invalid |
| `slo_experiment_last_invalid_timestamp_seconds` | when the last invalid run ended |
| `slo_experiment_last_run_timestamp_seconds`, `…_last_success_timestamp_seconds`, `…_last_duration_seconds` | when it last ran / passed, how long |
| `slo_experiment_expectation_met` | per expectation: fired in time (fire) or stayed quiet (quiet) |
| `slo_experiment_detection_seconds`, `slo_experiment_recovery_seconds` | fault → alert fired; fault removed → alert resolved |

## Safety

- **Lab clusters only.** The regression refuses to start unless every node has the label `slo-platform/lab=true`
  (`--lab-label`). Label the nodes of a lab when you build it; never label a cluster that serves users. A single
  `experiment.py` run has no such guard: check the printed context before you press enter.
- **Every fault ends by itself**, because the runner can die with its machine: Chaos Mesh objects need
  `spec.duration` ≥ the experiment's time, a fault-switch ConfigMap gets `expires`, anything else is refused, and so
  is a load Job without `activeDeadlineSeconds`. On SIGTERM the runner removes every fault before it exits.
- **One experiment per namespace.** The runner marks the namespace with the annotation `slo-platform/experiment`
  (`<name> <host> <start>`, also the lock) and, after the fault, `slo-platform/experiment-cooldown` (30 min: burn-rate
  alerts can fire after recovery; 45 min in the regression). A page in a marked namespace belongs to that experiment.
  A run killed hard leaves its mark; it expires after 4 h, or `kubectl annotate ns <ns> slo-platform/experiment-`.
- **Only namespaces that opt in.** Chaos Mesh acts only in namespaces annotated `chaos-mesh.org/inject: enabled`
  (every `<app>-<env>` namespace of a registered application), and the regression's service account may only act in
  those (a RoleBinding per application and environment).

## Reports that read the results

- `scripts/scorecard.py`: per application and prod SLO, the error budget left, the runbook anchor, the experiments
  whose `expect.fire` names the SLO with their last result (results files and the lab's `slo_experiment_*` metrics),
  pages and tickets; then the gaps (untested or unverified pages, failing or stale experiments, budget < 25%).
  `--context` / `--kubeconfig` (the scored cluster; default the current context), `--lab-context` (the lab, optional),
  `--runbook`, `--tz` (default: the system's).
- `scripts/alert-report.py`: every alert episode of a period, whether it would notify anyone (`--routing FILE`, an
  Alertmanager config in git with its history; default: the running Alertmanager's config), and whether it was
  useful: experiment (a results run on this context, the metrics, a namespace mark), incident, noise or unknown.

## The examples' experiments

| Experiment | Fault | Expected |
|---|---|---|
| sample-app-down | every sample-app pod fails | the probe SLO pages (no pod is left to count in-pod) |
| api-errors | api answers 503 (fault switch) | web and api availability page; web latency and probe quiet |
| api-slow (skipped) | api packets +800ms | superseded by -v2: every call timed out (an outage, not slowness) |
| api-slow-v2 (skipped) | api packets +300ms | superseded by -v3: 0.30 s per call, under the threshold |
| api-slow-v3 | api packets +600ms | web latency pages; web availability, probe and api latency quiet |
| api-pod-failure | one of two api pods fails 10 min | nothing pages |
| api-down | every api pod fails | web availability pages; api "SLI absent" ticket |
| quiet-traffic | none: 2 h of normal traffic | nothing pages (the negative test) |
| storefront-worker-one-down | one of two workers fails 10 min | nothing pages |
| storefront-worker-down | every worker fails | order freshness and completeness page; intake quiet |
| storefront-worker-slow | Postgres → workers +2 s per packet | nothing pages (slower, within the objective) |
| storefront-worker-overloaded | Postgres → workers +8 s per packet | order freshness pages; completeness quiet |
| storefront-db-unreachable (skipped) | Postgres → web and workers dropped | superseded by -v2: the absent ticket's 10 min was too tight |
| storefront-db-unreachable-v2 | the same | order intake pages; freshness "SLI absent" ticket within 13 min |

The reference installation also tested database backups (the recovery-point SLO pages when the object store is
unreachable; normal writes page nobody). Those experiments need the optional object store and a database with WAL
archiving; they are not shipped as examples.
