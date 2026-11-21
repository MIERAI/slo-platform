# bootstrap/

From an empty cluster to Argo CD running the platform, one cluster at a time. OpenTofu installs only what Argo CD
cannot install for itself; everything after that comes from Git:

1. **Argo CD** (pinned argo-cd chart; server without TLS, Dex and notifications off, Git polled every 30 s).
2. **Repository credentials** for the product and the instance repository, when a git token is given.
3. **Namespaces and Secrets**: `monitoring/<grafana.adminSecret>` (Grafana's admin login), the applications' Secrets
   from the secrets file, and `slo-experiments/git-credentials` on lab clusters (the daily regression clones both
   repositories).
4. **The root Application** `argocd/slo-platform`: the product's `platform/root` chart at `product.version`, with the
   instance's `platform.yaml` as its values. The chart renders every platform Application, the ApplicationSet for
   your `apps/*/app.yaml` — and the root Application itself, so from then on it is self-managed (see *Upgrade*).

| Directory | What |
|---|---|
| `tofu/kind` | creates a local kind cluster `slo-<cluster>`, then the platform on it |
| `tofu/cluster` | the platform on an existing cluster (k3s, a cloud cluster, …), by kubeconfig context |
| `tofu/modules/platform` | what both roots share (the four steps above) |

Both roots read `<instance_dir>/platform.yaml` (written by `bin/slo-platform init`, see docs/configuration.md) and,
when it exists, `<instance_dir>/secrets/<cluster>.sops.yaml`.

## Prerequisites

- [OpenTofu](https://opentofu.org) ≥ 1.10, kubectl.
- kind root: Docker with ≈ 8 GB of memory for the whole platform.
- An instance repository pushed to GitHub (`bin/slo-platform init`, commit, push): Argo CD reads `platform.yaml` and
  your apps from there, not from your disk.
- Private product or instance repository: a GitHub token that can read both (`gh auth token`, or a fine-grained
  token with *Contents: read*).
- A secrets file: [SOPS](https://github.com/getsops/sops) and [age](https://github.com/FiloSottile/age), and the
  age key where SOPS finds it (`SOPS_AGE_KEY_FILE` or `~/.config/sops/age/keys.txt`).

## Quickstart (kind)

From a checkout of this repository (at the same version as `product.version`, or newer). `instance_dir` and
`cluster` are needed by `init` too: they decide where the state lives (next section).

```sh
export TF_VAR_instance_dir=/absolute/path/to/my-instance   # your instance repository checkout
export TF_VAR_cluster=lab                                    # a key of clusters: in platform.yaml (runtime: kind)
export TF_VAR_git_token=$(gh auth token)                     # private repositories only; overrides git.token
tofu -chdir=bootstrap/tofu/kind init
tofu -chdir=bootstrap/tofu/kind apply                        # ~2 min: kind cluster + Argo CD + root Application
export KUBECONFIG=$TF_VAR_instance_dir/.secrets/lab/kubeconfig
kubectl -n argocd get applications                           # slo-platform, then the platform's Applications
```

The kubeconfig is written to `<instance_dir>/.secrets/<cluster>/kubeconfig` (context `kind-slo-<cluster>`);
`~/.kube/config` is never touched. Argo CD UI:
`kubectl -n argocd port-forward svc/argocd-server 8080:80` → http://localhost:8080, user `admin`, password in
`<instance_dir>/.secrets/<cluster>/argocd-admin.password`. Grafana's is next to it (`grafana-admin.password`).

Switching to another cluster or instance directory: export the new values and run
`tofu -chdir=… init -reconfigure` (OpenTofu refuses to plan against a changed backend until then — it never mixes
two states).

## An existing cluster

Same, with the `cluster` root and the cluster's kubeconfig context (required: the current context is never used
implicitly; `TF_VAR_kubeconfig` defaults to `~/.kube/config`):

```sh
export TF_VAR_instance_dir=/absolute/path/to/my-instance TF_VAR_cluster=prod TF_VAR_context=my-prod
tofu -chdir=bootstrap/tofu/cluster init
tofu -chdir=bootstrap/tofu/cluster apply
```

`runtime:` in platform.yaml must match the cluster (`k3s` or `generic`): Chaos Mesh uses it to find the container
runtime's socket.

### Labelling lab nodes

Fault experiments and the daily regression refuse to run unless **every** node carries `slo-platform/lab=true`. The
kind root sets it on lab clusters; on any other cluster label the nodes yourself (and new nodes as they join, e.g.
through your node pool's labels or k3s `--node-label`):

```sh
kubectl --context my-lab label nodes --all slo-platform/lab=true
```

`apply` of the `cluster` root warns (check `lab_nodes_labelled`) when the role is `lab` and a node is not labelled.
Never label a production cluster's nodes.

## The secrets file

`<instance_dir>/secrets/<cluster>.sops.yaml` (another path: `TF_VAR_secrets_file`). Everything is optional; without
the file the cluster runs on generated throwaway values.

```yaml
git: { token: ghp_… }              # Argo CD repository credentials (product + instance) and the regression's clone
grafana: { adminPassword: … }      # → Secret monitoring/<grafana.adminSecret>; random when absent
secrets:                           # your applications' Secrets: namespace → Secret name → key/value
  shop-prod: { db-credentials: { username: shop, password: … } }
```

Create and edit it with SOPS (`.sops.yaml` in the instance holds the age recipients): `sops secrets/prod.sops.yaml`.
Namespaces named there are created by bootstrap if missing. `TF_VAR_git_token` takes precedence over `git.token`.

## State and its sensitivity

The state is local: `<instance_dir>/.tofu/<cluster>.tfstate` (a `backend "local"` whose path uses `instance_dir` and
`cluster` — hence both variables are needed at `init`). `init` of the instance gitignores `.tofu/` and `.secrets/`.

**The state holds every secret in plain text**: the git token, both admin passwords, the applications' Secrets.
Treat it like the age key: never commit it, back it up only encrypted. OpenTofu writes it with your umask (usually
0644): `chmod 700 .tofu .secrets` in the instance keeps other local users out. To encrypt it at rest, use OpenTofu's state
encryption, e.g. with a passphrase kept outside the instance:

```sh
export TF_ENCRYPTION='key_provider "pbkdf2" "k" { passphrase = "…" }
method "aes_gcm" "m" { keys = key_provider.pbkdf2.k }
state { method = method.aes_gcm.m }'
```

(add `enforced = true` inside `state { }` once the state is encrypted).

## Upgrade

Edit `product.version` in `platform.yaml` (or `bin/slo-platform upgrade --version <ref>`), commit, push. The root
Application renders itself from that version, so Argo CD retargets it and everything it renders within a minute —
no `tofu apply` needed. The root Application in the state is created once and ignored afterwards; Argo CD's copy
owns it.

`tofu apply` again is for what bootstrap owns: a new Secret in the secrets file, a rotated git token, a newer Argo CD
chart (`TF_VAR_argocd_chart_version`).

## Destroy

```sh
tofu -chdir=bootstrap/tofu/kind destroy        # deletes the kind cluster; state and .secrets/<cluster> stay local
```

On an existing cluster, `tofu -chdir=bootstrap/tofu/cluster destroy` removes Argo CD, the root Application, the
repository credentials, the Secrets and the namespaces bootstrap created (`monitoring` among them, with everything in
it). Applications the root Application rendered are not cascaded: delete them in Argo CD first if the platform's
workloads should go too.
