# The platform on one cluster: Argo CD → secrets (namespaces + Secrets) → the root Application.
# Everything after that is Argo CD's: the root Application syncs the product's platform/root chart at product.version,
# which renders every platform Application, the ApplicationSet for the instance's apps and the root Application itself.

locals {
  platform_yaml_ok = try(var.platform.apiVersion, "") == "slo-platform/v1"
  role             = try(var.platform.clusters[var.cluster].role, "")
  lab              = local.role == "lab"

  product_repo      = try(var.platform.product.repo, "")
  product_version   = try(var.platform.product.version, "")
  instance_repo     = try(var.platform.instance.repo, "")
  instance_revision = try(var.platform.instance.revision, "main")
  grafana_secret    = try(var.platform.grafana.adminSecret, "grafana-admin")

  secrets_dir = "${var.instance_dir}/.secrets/${var.cluster}"
  labels      = merge({ "app.kubernetes.io/part-of" = "slo-platform" }, var.labels)

  # Secret values stay sensitive; only whether they exist and the namespace / Secret names are not
  git_token        = var.git_token != "" ? var.git_token : try(tostring(var.secrets.git.token), "")
  has_git_token    = nonsensitive(local.git_token != "")
  grafana_password = try(tostring(var.secrets.grafana.adminPassword), "")
  notify_secrets   = try({ for k, v in var.secrets.notify : k => tostring(v) }, {})
  generic_secrets = nonsensitive(toset(flatten([
    for ns in try(keys(var.secrets.secrets), []) : [for name in try(keys(var.secrets.secrets[ns]), []) : "${ns}/${name}"]
  ])))

  repositories = toset(distinct([local.product_repo, local.instance_repo])) # one credential per URL (they may coincide)
  namespaces = setsubtract(toset(concat(
    ["monitoring"],
    [for k in local.generic_secrets : split("/", k)[0]],
    local.lab && local.has_git_token ? ["slo-experiments"] : [],
  )), ["argocd"]) # argocd: created by the Helm release
}

# ── Argo CD ──────────────────────────────────────────────────────────────────────────────────────────────────────────
resource "helm_release" "argocd" {
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_chart_version
  wait             = true
  timeout          = 600
  values = [yamlencode({
    fullnameOverride = "argocd" # the object names of the upstream install manifests (argocd-server, …)
    configs = {
      cm     = { "timeout.reconciliation" = "30s" } # no webhook: poll Git every 30s
      params = { "server.insecure" = true }         # UI through port-forward, no TLS
    }
    dex           = { enabled = false } # admin login only
    notifications = { enabled = false } # the platform notifies through Alertmanager
    # Argo CD's own NetworkPolicies stay off: some CNIs (kindnet) then drop all ingress to the selected pods
    global = { networkPolicy = { create = false } }
  })]

  lifecycle {
    precondition {
      condition     = local.platform_yaml_ok
      error_message = "platform.yaml: apiVersion must be slo-platform/v1."
    }
    precondition {
      condition     = contains(["prod", "lab"], local.role)
      error_message = "platform.yaml: clusters.${var.cluster} is missing or its role is not prod | lab."
    }
    precondition {
      condition     = local.product_repo != "" && local.product_version != "" && local.instance_repo != ""
      error_message = "platform.yaml: product.repo, product.version and instance.repo are required."
    }
  }
}

# ── Argo CD repository credentials (private product / instance repositories) ────────────────────────────────────────
resource "kubernetes_secret_v1" "repository" {
  for_each = local.has_git_token ? local.repositories : toset([])
  metadata {
    name      = "repo-${substr(sha1(each.key), 0, 10)}"
    namespace = helm_release.argocd.namespace
    labels    = merge(local.labels, { "argocd.argoproj.io/secret-type" = "repository" })
  }
  data = {
    type     = "git"
    url      = each.key
    username = "x-access-token"
    password = local.git_token
  }
}

# ── Namespaces and Secrets the platform and the applications reference ────────────────────────────────────────────
# Created before the first sync (the workloads need the Secrets); Argo CD adds its own labels later.
resource "kubernetes_namespace_v1" "ns" {
  for_each = local.namespaces
  metadata {
    name   = each.key
    labels = local.labels
  }
  lifecycle { ignore_changes = [metadata[0].labels, metadata[0].annotations] }
}

resource "random_password" "grafana" {
  length  = 24
  special = false
}

resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = local.grafana_secret
    namespace = kubernetes_namespace_v1.ns["monitoring"].metadata[0].name
    labels    = local.labels
  }
  data = {
    "admin-user"     = "admin"
    "admin-password" = local.grafana_password != "" ? local.grafana_password : random_password.grafana.result
  }
}

# notify.<key>: the notification receivers' secrets (bot tokens, webhook URLs, SMTP password, …). Always created, empty
# without any: Alertmanager mounts it (alertmanagerSpec.secrets) as soon as platform.yaml's notify has a receiver that
# reads one of its keys, and a missing Secret would keep Alertmanager from starting. A missing key only fails that
# receiver (`slo-platform check` warns about it).
resource "kubernetes_secret_v1" "alertmanager_notify" {
  metadata {
    name      = "alertmanager-notify"
    namespace = kubernetes_namespace_v1.ns["monitoring"].metadata[0].name
    labels    = local.labels
  }
  data = local.notify_secrets
}

# The daily regression clones the product and the instance; private repositories need the token (lab clusters only)
resource "kubernetes_secret_v1" "experiments_git" {
  count = local.lab && local.has_git_token ? 1 : 0
  metadata {
    name      = "git-credentials"
    namespace = kubernetes_namespace_v1.ns["slo-experiments"].metadata[0].name
    labels    = local.labels
  }
  data = { token = local.git_token }
}

# secrets.<namespace>.<name>.<key>: the applications' Secrets
resource "kubernetes_secret_v1" "generic" {
  for_each = local.generic_secrets
  metadata {
    name      = split("/", each.key)[1]
    namespace = split("/", each.key)[0]
    labels    = local.labels
  }
  data       = { for k, v in var.secrets.secrets[split("/", each.key)[0]][split("/", each.key)[1]] : k => tostring(v) }
  depends_on = [kubernetes_namespace_v1.ns, helm_release.argocd]
}

# ── The root Application ────────────────────────────────────────────────────────────────────────────────────────────
# Created once, exactly as platform/root renders it (P2 contract). From the first sync on, the chart's copy owns the
# object (self-managed: a version change in platform.yaml retargets it), so later changes here are ignored.
resource "helm_release" "root" {
  name       = "slo-platform-root"
  namespace  = helm_release.argocd.namespace
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argocd-apps"
  version    = var.argocd_apps_chart_version
  values = [yamlencode({ applications = { "slo-platform" = {
    namespace = "argocd"
    project   = "default"
    sources = [
      {
        repoURL        = local.product_repo
        targetRevision = local.product_version
        path           = "platform/root"
        helm = {
          valueFiles = ["$instance/platform.yaml"]
          parameters = [{ name = "cluster", value = var.cluster }]
        }
      },
      { repoURL = local.instance_repo, targetRevision = local.instance_revision, ref = "instance" },
    ]
    destination = { server = "https://kubernetes.default.svc", namespace = "argocd" }
    syncPolicy  = { automated = { prune = true, selfHeal = true } }
  } } })]

  lifecycle { ignore_changes = [values, version] }
  depends_on = [kubernetes_secret_v1.repository, kubernetes_secret_v1.grafana_admin, kubernetes_secret_v1.alertmanager_notify,
  kubernetes_secret_v1.experiments_git, kubernetes_secret_v1.generic]
}

# ── Local copies for the operator: <instance_dir>/.secrets/<cluster>/ (gitignored by init) ──────────────────────────
data "kubernetes_secret_v1" "argocd_admin" {
  metadata {
    name      = "argocd-initial-admin-secret"
    namespace = helm_release.argocd.namespace
  }
}

resource "local_sensitive_file" "argocd_admin" {
  filename             = "${local.secrets_dir}/argocd-admin.password"
  content              = try(data.kubernetes_secret_v1.argocd_admin.data.password, "")
  file_permission      = "0600"
  directory_permission = "0700"
}

resource "local_sensitive_file" "grafana_admin" {
  filename             = "${local.secrets_dir}/grafana-admin.password"
  content              = kubernetes_secret_v1.grafana_admin.data["admin-password"]
  file_permission      = "0600"
  directory_permission = "0700"
}
