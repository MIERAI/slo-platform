# Quickstart

From nothing to the first passing fault experiment on a local kind cluster: about 15 minutes of commands and waiting
for the platform, then a 15–25 minute experiment. You will

1. create your **instance repository** (your configuration + the example application `sample-app`) and push it,
2. bootstrap a kind cluster with OpenTofu: Argo CD, then everything else from Git,
3. break `sample-app` on purpose and watch the platform prove that its SLO alert pages in time.

The same path runs in CI on every release (`scripts/e2e.sh`, see [End-to-end test](#end-to-end-test)).

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker | Docker Desktop 4.x (macOS) or Docker Engine 25+ (Linux) | **≥ 8 GB memory and 4 CPUs** for Docker (Docker Desktop: Settings → Resources) |
| [OpenTofu](https://opentofu.org/docs/intro/install/) | ≥ 1.10 (tested: 1.13) | creates the kind cluster itself: no `kind` CLI needed |
| kubectl | within one minor version of the cluster (Kubernetes 1.36) | |
| git | any recent | |
| Python | ≥ 3.9 with PyYAML | Linux: `apt install python3-yaml`. macOS (Homebrew's Python refuses `pip install` system-wide, PEP 668): a virtualenv — `python3 -m venv ~/.venvs/slo && ~/.venvs/slo/bin/pip install pyyaml`, then run the tools with `~/.venvs/slo/bin/python bin/slo-platform …` — or with [uv](https://docs.astral.sh/uv/): `uv run --with pyyaml bin/slo-platform …` |
| [gh](https://cli.github.com) | optional | creating the repository and a token in one command each |

A GitHub account for the instance repository. A token is only needed when the instance repository is private: Argo CD
reads it with that token (`gh auth token`, or a fine-grained token with *Contents: read* on it).

Linux only: kind runs many pods in one container and the default inotify limits are too small (symptom: pods crash
with `too many open files`):

```sh
sudo sysctl fs.inotify.max_user_instances=512 fs.inotify.max_user_watches=524288
```

## 1. The instance repository (2 min)

Get the product (the platform's code and the tools) and create your instance next to it. Replace `<you>` with your
GitHub user or organisation:

```sh
git clone https://github.com/MIERAI/slo-platform.git && cd slo-platform
bin/slo-platform init ../my-slo --owner <you> --repo my-slo        # cluster "lab" (role lab, kind), sample-app
bin/slo-platform check ../my-slo
```

`init` wrote `../my-slo/platform.yaml` (the one configuration file, reference: [configuration.md](configuration.md))
and copied `examples/sample-app` to `../my-slo/apps/sample-app` — its `app.yaml` declares three SLOs and synthetic
users, and `experiments/` holds the fault experiments that verify them.

Push it to GitHub: Argo CD reads your configuration from there, not from your disk.

```sh
cd ../my-slo
git init -b main && git add -A && git commit -m "slo-platform instance"
gh repo create <you>/my-slo --private --source . --push     # (gh set to git_protocol ssh needs a working SSH key) or create it on github.com, then:
# git remote add origin https://github.com/<you>/my-slo.git && git push -u origin main
cd ../slo-platform
```

## 2. Bootstrap the cluster (3 min + 10 min of Argo CD)

```sh
export TF_VAR_instance_dir=$(cd ../my-slo && pwd)    # absolute path of the instance checkout
export TF_VAR_cluster=lab                            # the key under clusters: in platform.yaml
export TF_VAR_git_token=$(gh auth token)             # private repositories only
tofu -chdir=bootstrap/tofu/kind init
tofu -chdir=bootstrap/tofu/kind apply                # answer "yes"; creates the kind cluster slo-lab
export KUBECONFIG=$TF_VAR_instance_dir/.secrets/lab/kubeconfig
```

`apply` creates the kind cluster `slo-lab`, installs Argo CD and one Application, `slo-platform`. Your
`~/.kube/config` is not touched: the kubeconfig is written into the instance (`.secrets/`, gitignored), so every
command below uses `KUBECONFIG` from the last line. OpenTofu's state is in `.tofu/` of the instance and holds the
token and passwords in plain text — it is gitignored; keep it that way ([bootstrap/README.md](../bootstrap/README.md)).

## 3. Watch Argo CD install the rest (≈ 10 min)

```sh
kubectl -n argocd get applications -w
```

`slo-platform` renders the platform's Applications, which install Prometheus + Alertmanager + Grafana, Pyrra, the
blackbox prober, Chaos Mesh, Argo Rollouts, cert-manager, CloudNativePG and the daily experiment regression; the
ApplicationSet turns `apps/sample-app/app.yaml` into six more (the app, its SLOs and its synthetic users, in `dev` and
`prod`). Done when all **15** show `Synced` and `Healthy`:

```
NAME                       SYNC STATUS   HEALTH STATUS
platform-argo-rollouts     Synced        Healthy
platform-blackbox          Synced        Healthy
platform-cert-manager      Synced        Healthy
platform-chaos-mesh        Synced        Healthy
platform-cnpg              Synced        Healthy
platform-experiments       Synced        Healthy
platform-monitoring        Synced        Healthy
platform-pyrra             Synced        Healthy
sample-app-dev             Synced        Healthy
sample-app-prod            Synced        Healthy
sample-app-slo-dev         Synced        Healthy
sample-app-slo-prod        Synced        Healthy
sample-app-traffic-dev     Synced        Healthy
sample-app-traffic-prod    Synced        Healthy
slo-platform               Synced        Healthy
```

`Progressing`, `OutOfSync` or `Missing` for a few minutes is normal while images are pulled and CRDs appear
(Argo CD retries). Still not there after 20 minutes: [Troubleshooting](#troubleshooting).

The UIs, each in its own terminal (user `admin`; passwords in `$TF_VAR_instance_dir/.secrets/lab/`):

```sh
kubectl -n argocd port-forward svc/argocd-server 8080:80          # http://localhost:8080  (argocd-admin.password)
kubectl -n monitoring port-forward svc/platform-monitoring-grafana 3000:80        # http://localhost:3000  (grafana-admin.password)
kubectl -n monitoring port-forward svc/pyrra-api 9099:9099        # http://localhost:9099  the SLOs and their budgets
```

In Pyrra you find six objectives: `sample-app-hello-availability`, `-latency` and `-probe`, once per environment.

## 4. The first experiment (15–25 min)

`apps/sample-app/experiments/sample-app-down.yaml` states a hypothesis and a prediction, written down before the run:
*when every sample-app pod fails, the probe SLO pages (ErrorBudgetBurn, severity page, 1h window) within 10 minutes —
and nothing else pages.* Let it run once the probe has had a few minutes of data (a couple of minutes after all
Applications are healthy):

```sh
python3 scripts/experiment.py --kubeconfig $KUBECONFIG \
  $TF_VAR_instance_dir/apps/sample-app/experiments/sample-app-down.yaml
```

The runner prints what it does and what it sees, every 15 s:

```
hh:mm:ss [sample-app-down] hypothesis: Every sample-app pod fails — …
hh:mm:ss [sample-app-down] fault created in sample-app-dev: PodChaos/sample-app-all-down
hh:mm:ss [sample-app-down] ✓ fault takes effect (40s): max(probe_success{namespace="sample-app-dev"}) == 0
hh:mm:ss [sample-app-down] ✓ fired after 4.3 min: ErrorBudgetBurn {'namespace': 'sample-app-dev', 'slo': 'sample-app-hello-probe', …}
hh:mm:ss [sample-app-down] fault removed after 4.4 min
hh:mm:ss [sample-app-down] ✓ resolved 6.1 min after the fault: ErrorBudgetBurn …
hh:mm:ss [sample-app-down] PASS
```

(the minutes vary). Exit code 0 = PASS. Watch it from the other side meanwhile: Grafana's Alerting page or
`kubectl -n monitoring port-forward svc/kps-alertmanager 9093:9093` show the page firing — on a lab cluster
Alertmanager notifies nobody, the experiment is the consumer.

### Reading the result

The report is `apps/sample-app/experiments/results/<date>-sample-app-down.md` in your instance (or `--results DIR`):
the hypothesis, the kubectl context it ran against, and the log above. Commit it
(`git add apps/*/experiments/results && git commit -m "experiment results" && git push`): results are the record of
which alerts have been seen working, and the scorecard reads them.

| Verdict | Means | Then |
|---|---|---|
| **PASS** | every expected alert fired in time, nothing else paged, the alerts resolved after the fault | your alerting for that failure is verified, not hoped for |
| **FAIL** | the line with ✗ says which prediction broke: did not fire (in time), something unexpected paged, still firing after `recover` | a real finding about the SLO or its alert (or the experiment's prediction): [runbook](runbook.md#experiment-regression) |
| **INVALID** | time did not flow normally (the laptop slept, Prometheus had a gap): says nothing about the alerts | run it again with the machine awake |

A second run on the same day appends a section to the same report. The same experiments run every day on their own on
a lab cluster (`regression.schedule` in platform.yaml; results as metrics, a ticket when one fails): see
[experiments.md](experiments.md).

Next: write your own application's `app.yaml` ([app-yaml.md](app-yaml.md)) and experiments
([experiments.md](experiments.md)), commit, push — Argo CD picks them up within a minute.

## Optional: pages on your phone (ntfy)

The quickstart's cluster has `role: lab`, and **a lab cluster notifies nobody** — whatever `notify` says: its alerts
are what the experiments check. Notifications go out only from clusters with `role: prod` (and only for SLO alerts of
namespaces not ending in `-dev` / `-staging`). So this step adds a second, prod cluster — on kind, to try it:

1. Install the [ntfy](https://ntfy.sh) app and subscribe to a topic with a long random name (on the public server
   anyone who knows the name can read it), e.g. `slo-pages-$(openssl rand -hex 8)`.
2. In `platform.yaml` of your instance, add the cluster and replace `notify: {}` with a receiver (reference:
   [configuration.md](configuration.md#notify)), then `bin/slo-platform check`, commit, push:

   ```yaml
   clusters:
     lab: { role: lab, runtime: kind, objectStorage: none }
     demo: { role: prod, runtime: kind, objectStorage: none }   # notifies; never runs experiments
   notify:
     page:
       - { type: ntfy, topic: slo-pages-<random> }
   ```

3. Bootstrap it like the lab (a second kind cluster `slo-demo`: give Docker another 8 GB, or
   `tofu -chdir=bootstrap/tofu/kind destroy` the lab first):

   ```sh
   export TF_VAR_cluster=demo KUBECONFIG=$TF_VAR_instance_dir/.secrets/demo/kubeconfig
   tofu -chdir=bootstrap/tofu/kind init -reconfigure     # another cluster: another state
   tofu -chdir=bootstrap/tofu/kind apply
   ```

4. Once `platform-monitoring` is Healthy, send Alertmanager a test page — it takes the real route, so it reaches the
   phone exactly as a real fast burn of sample-app's probe SLO in prod would (within ~30 s, `[PAGE] sample-app-prod:
   …`; it resolves on its own after 5 minutes):

   ```sh
   kubectl -n monitoring exec alertmanager-kps-alertmanager-0 -c alertmanager -- amtool alert add \
     alertname=ErrorBudgetBurn severity=page long=1h namespace=sample-app-prod slo=sample-app-hello-probe \
     --annotation=summary="test page from the quickstart" --alertmanager.url=http://localhost:9093
   ```

Nothing on the phone: [Troubleshooting](#troubleshooting) (`kubectl -n monitoring logs alertmanager-kps-alertmanager-0
-c alertmanager | grep -i notify`). Fault experiments stay on the lab cluster; prod gets the same SLOs and alerts, and
its pages reach you.

## 5. Clean up

```sh
tofu -chdir=bootstrap/tofu/kind destroy        # deletes the kind cluster slo-lab
```

The instance directory keeps its state and `.secrets/lab/` (delete them if you are done with this cluster). Did the
ntfy step? `TF_VAR_cluster=demo`, `init -reconfigure` and `destroy` again for `slo-demo`. The
GitHub repository is yours to keep or delete (`gh repo delete <you>/my-slo`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `tofu init`: "Variables not allowed" / backend errors | `TF_VAR_instance_dir` and `TF_VAR_cluster` are needed at `init` too (the state lives in the instance) | export both, then `tofu -chdir=bootstrap/tofu/kind init` |
| `tofu plan`/`apply`: "Backend configuration changed" | another instance or cluster than at the last `init` | `tofu -chdir=bootstrap/tofu/kind init -reconfigure` |
| `instance_dir must be an absolute path` | a relative `TF_VAR_instance_dir` | `$(cd ../my-slo && pwd)` |
| `clusters.lab.runtime must be kind` | the cluster in platform.yaml is not a kind cluster | `--cluster lab:kind` at `init`, or `bootstrap/tofu/cluster` for an existing cluster |
| `slo-platform` stays `Unknown`, condition `ComparisonError … authentication required` / `repository not found` | Argo CD cannot read the product or the instance repository | private repository: `TF_VAR_git_token` and `tofu apply` again; check `instance.repo` in platform.yaml and that you pushed `main` |
| `slo-platform` Synced, but no `sample-app-*` Applications | the ApplicationSet finds no `apps/*/app.yaml` at `instance.revision` | `kubectl -n argocd describe applicationset applications`; push the instance |
| Pods `Pending` (`Insufficient memory/cpu`) or `OOMKilled`, Applications `Progressing` for long | Docker has too little memory | Docker Desktop → Resources: ≥ 8 GB; `tofu destroy`, `apply` again |
| Pods `CrashLoopBackOff` with `too many open files` (Linux) | inotify limits | the `sysctl` under [Prerequisites](#prerequisites) |
| `ErrImagePull` / `ImagePullBackOff` | no network, or a registry rate limit (Docker Hub) | wait and let Argo CD retry; check `kubectl get events -A --field-selector type=Warning` |
| `traffic` pod logs `connection refused` in its first seconds | the synthetic users start before sample-app's pods are ready | harmless: requests that never reach a pod are not counted by the in-pod SLIs; it stops once the app is up |
| experiment: `expected alert already firing before the fault` | the probe SLO is still burning from the install (the app was not up yet while the prober already ran) | wait until `ErrorBudgetBurn` for `sample-app-dev` resolves (Alertmanager / Grafana), then run again |
| experiment: `sample-app-dev is busy` | another run holds the namespace's lock (or a killed one, for up to 4 h) | wait; or remove the annotation `slo-platform/experiment` from the namespace if you are sure no run is active |
| experiment: `fault injected but no effect within 3m` | the probe has no data yet (Prometheus not yet scraping it) | wait a few minutes after the install, run again |
| ntfy: nothing arrives | a lab cluster (never notifies), a `-dev` namespace (never notifies), another topic, or ntfy refused it | `role: prod`; the test page's namespace `sample-app-prod`; Alertmanager's log (`… -c alertmanager \| grep -i notify`) |
| experiment INVALID | the machine slept or Prometheus had a gap | run again, machine awake (`caffeinate` is used automatically on macOS; a closed lid still sleeps) |

Everything in one view: `kubectl -n argocd get applications`, `kubectl get pods -A | grep -v -e Running -e Completed`,
`kubectl get events -A --sort-by=.lastTimestamp | tail -30`. `scripts/e2e.sh` collects the same into a directory
when it fails.

## End-to-end test

`scripts/e2e.sh` runs this quickstart unattended on a throwaway kind cluster `slo-e2e`, without GitHub: it runs a small
git server inside the cluster (`scripts/e2e/git-server.yaml`), pushes this checkout's working tree (uncommitted
changes included) and an instance made by `init` into it, bootstraps with `bootstrap/tofu/kind`, waits until every
Application is Synced and Healthy, and with `--experiment` runs `sample-app-down`, which must PASS. It deletes the
cluster at the end unless `--keep`; on failure it prints a diagnosis and writes it to `--diag DIR` (default
`./e2e-diagnostics`).

```sh
scripts/e2e.sh --experiment          # ≈ 15 min + the experiment; needs Docker, tofu, kubectl, git, Python + PyYAML
```

CI (`.github/workflows/e2e.yml`) runs it weekly, on release tags (with the experiment), on pull requests that change
it, and on demand (Actions → e2e → Run workflow).
