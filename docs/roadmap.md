# Roadmap

slo-platform is extracted from a working personal installation (three repositories: bootstrap, manifests, principles)
into one versioned product. The personal installation then becomes its first user.

| Phase | Scope | Done when |
|---|---|---|
| **P0** ✓ | Repository, layout, license, contribution / security policy, CI skeleton, the configuration draft | this repository exists with the layout and a green CI |
| **P1** ✓ | Move the generic platform over; no application named in platform code (permissions and traffic by label / app.yaml); home-lab extras stay out | validation green with the examples deleted |
| **P2** ✓ | `platform.yaml` + `init` (instance repository pinned to a release); `bootstrap/tofu` for any cluster; secrets init | a fresh owner instantiates on kind with zero leftovers |
| **P3** ✓ | Notification receivers as a slot (ntfy, Slack, webhook, email); default routing and inhibition in the platform | a page reaches ntfy from the quickstart |
| **P4** ✓ | Quickstart, configuration reference, runbook; CI instantiates the template end to end on kind (and k3s) | a newcomer sees the first passing experiment in 30 minutes; **v0.1.0** |
| **P5** | The reference installation switches to v0.1.0 | the personal clusters run a released version |
| **P6** | Experiments generated per SLI type for every declared SLO | declaring an SLO is enough to have it verified daily |

Later: OpenSLO import / export, more SLI types, a UI for experiment results.
