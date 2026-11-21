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
- every `apps/*/app.yaml`: `name` is the directory name (a DNS label); `environments` is a non-empty list of unique
  names, `trafficScale` ≥ 0, `promotion.from` an earlier environment; `components` unique, each exactly one of
  `kustomize` / `helm`, not `<app>-slo` / `<app>-traffic`, and their files exist for every environment
  (`<dir>/<env>/kustomization.yaml`; `<dir>/chart/Chart.yaml` and `<dir>/values-<env>.yaml`); `services` name a
  component; every SLO has a unique name, an objective between 0 and 100, **exactly one SLI type** with its required
  fields, and `environments` among the application's; `traffic` has `peakRpm` > 0 and requests with `weight` > 0,
  `method`, `service`, `port`, a `path` starting with `/`, `badRate` between 0 and 1.
- every `apps/*/experiments/*.yaml`: a unique name across applications; `namespace` is `<app>-<env>` of an existing
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
| `timezone` | `Etc/UTC` | IANA name: the daily regression's CronJob `timeZone`, the synthetic users' day (below) |
| `language` | `en` | `en`, `zh`, `ja`: notification texts (`notify.language` overrides it) |
| `clusters.<name>.role` | — (required) | `prod`: notifies, never runs experiments. `lab`: fault experiments and the daily regression; notifies nobody |
| `clusters.<name>.runtime` | `generic` | `kind`, `k3s`, `generic`: Chaos Mesh's container runtime socket (`/run/k3s/containerd/containerd.sock` on k3s, `/run/containerd/containerd.sock` otherwise) |
| `clusters.<name>.objectStorage` | `none` | `in-cluster`: an S3-compatible store (SeaweedFS) for database backups |
| `optional.policies` | `false` | Kyverno and the workload baseline (admission policies in application namespaces) |
| `grafana.adminSecret` | `grafana-admin` | Secret in `monitoring` with keys `admin-user`, `admin-password`; bootstrap creates it |
| `regression.schedule` | `0 9 * * *` | Cron schedule of the daily experiment regression on lab clusters, in `timezone` |
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
