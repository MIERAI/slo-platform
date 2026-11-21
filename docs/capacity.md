# Capacity tests

How much load does an environment take before users notice? The answer is a rate, measured against the **SLOs**, not
a CPU percentage: "the CPU is at 80%" says nothing about whether pages are still fast. `scripts/capacity.py` raises
the load step by step until an SLO stops holding — the **knee** — and reports:

- the knee (req/s that still held) and the first rate that broke, with the SLI that broke;
- req/s per pod of the first workload, and its CPU per pod at the knee;
- a recommended ceiling (knee × `headroom`, default 70%);
- what ran out first: CPU of each workload and of the nodes, last good step against the first breaking one;
- optional: how long the autoscaler takes to catch a jump (the HPA's decision, the new pods ready, the SLI back).

It runs on a **lab** only (every node labelled `slo-platform/lab=true`) and marks the namespace like a fault
experiment, so the regression and other experiments wait, and alerts that fire there belong to the test.

## app.yaml

```yaml
capacity:
  slos: [web-availability, web-latency]   # judged per step (default: every availability / latency / probe / ratio SLO)
  workloads: [web, api]                    # Deployments reported (pods, CPU); the first is what "per pod" means
  requests:                                # default: traffic.requests (same format)
    - { weight: 90, method: GET, service: web, port: 8080, path: "/" }
    - { weight: 10, method: POST, service: web, port: 8080, path: /orders, body: '{"amount": 1{rand}}' }
  ramp: { start: 10, factor: 2, max: 2000, step: 90s, warmup: 30s, refine: 3, headroom: 0.7 }
  autoscaling: { workload: web, from: 0.5, to: 1.5, hold: 2m, watch: 8m }   # optional
```

- **ramp**: rates `start, start×factor, …` up to `max` (req/s), each held `step`; the SLIs of a step are computed
  over the step minus `warmup` (connections, caches, the first scrape). After the first breaking step: a 60s cool-down,
  then `refine` evenly spaced rates between the last good and the first bad one. `step − warmup` must be ≥ 45s.
- **SLIs** use each SLO's own definition and objective (the same queries Pyrra records: `platform/slo`). `age` and
  `bool` SLOs have no per-step meaning and cannot be listed.
- **autoscaling**: needs a HorizontalPodAutoscaler on the workload (and, under Argo CD, `ignoreDifferences` on its
  `/spec/replicas`, or self-heal undoes every scale-up — see below). Holds `from × knee`, jumps to `to × knee`, polls
  the HPA every 5s.

## Running it

```sh
python3 scripts/capacity.py --kubeconfig ~/.kube/lab.yaml \
  --k6 "ssh loadgen k6" --address <a node address the generator reaches> \
  <apps>/<app>/app.yaml staging
```

- `--k6` is the shell command that runs k6 (the script goes in on stdin: `k6 run -`). Put the generator on a machine
  **outside** the cluster: in the cluster it competes for the CPU it is measuring.
- The requests go to a temporary NodePort Service `<service>-capacity` per target service (label
  `slo-platform/capacity=true`), deleted at the end, at `--address` (default: the first node's InternalIP).
- Each step is judged **holds** (every SLI ≥ objective), **breaks** (one is not, or nothing counted the requests), or
  **limited**: the server held but k6 did not send the rate (dropped iterations, or < 90% of it). Limited stops the
  test: above that rate it would measure the generator or the network, not the application, and the report says
  "capacity ≥ N" instead of inventing a knee.

Report: `capacity/results/<date>-<app>-<env>-capacity.md` and `.json` next to app.yaml (`--results DIR`). Exit 0: a
knee was found; 2: none (limited, or `max` held); 1: could not run.

## Reading the result

- **Knee and per pod** hold for *this* configuration: replicas, requests and limits, the node it ran on. A workload
  without a CPU limit can use the whole node, so "per pod" then means "per pod on an otherwise idle node".
- **What ran out**: the workload whose CPU grew most between the last good and the first breaking step is the first
  suspect; nodes near 100% mean the cluster, not the service, is the limit. CPU that did not grow while an SLO broke
  points at something else — a lock, a pool, the database, the network: the next question is *which hop*, which is
  what traces answer.
- **A workload that dies under load is not "limited"**: when pods are OOMKilled or crash-loop, nothing is left to count
  the failed requests, and in-pod SLIs (availability, latency from the pods' own counters) stay at 100% while users get
  timeouts. Each step therefore also breaks on: no ready pod of a reported workload during the step, any restart
  (OOMKilled named), or k6's own failure rate above the strictest objective. Found on the reference installation:
  storefront web (memory limit 64Mi) OOMKilled at 1280 req/s, its availability SLI at 1.0000 throughout.
- **Neighbours**: every step reports the CPU outside the tested namespace — other namespaces and the host itself
  (kubelet / k3s, the kernel's network path, swapping). On a small shared lab node these can decide the knee; runs at
  the knee and the first break are repeated (`confirm`), and disagreeing runs mark the result **unstable**.
- **Recommended** (knee × headroom) is the rate to plan with: the margin covers a pod lost, a noisy neighbour, a
  deploy in progress.

## Autoscaling under Argo CD

An HPA changes `spec.replicas`; Argo CD's self-heal sets it back to the manifest's value. Tell Argo CD to ignore it in
app.yaml (passed through with `RespectIgnoreDifferences`):

```yaml
ignoreDifferences:
  - { group: apps, kind: Deployment, name: web, jsonPointers: [/spec/replicas] }
```
