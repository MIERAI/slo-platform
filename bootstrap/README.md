# bootstrap/

From an empty cluster to Argo CD running the platform (P2):

- `init` — reads `platform.yaml`, creates your instance repository (your config, your apps, a pinned reference to a
  slo-platform release) and the secrets skeleton.
- `tofu/` — Argo CD + runtime secrets + the root application, for one cluster at a time (kind, k3s, any Kubernetes).
- Secrets: SOPS + age, initialised by a script; without a secrets file, throwaway values are generated (quickstart).
