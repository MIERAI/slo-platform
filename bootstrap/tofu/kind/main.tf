# A local kind cluster "slo-<cluster>" and the platform on it.
#   export TF_VAR_instance_dir=/abs/path/to/instance TF_VAR_cluster=lab
#   tofu -chdir=bootstrap/tofu/kind init && tofu -chdir=bootstrap/tofu/kind apply      (bootstrap/README.md)

variable "node_image" {
  description = "kindest/node image (the Kubernetes version)"
  type        = string
  default     = "kindest/node:v1.36.1"
}

locals {
  kubeconfig = "${local.secrets_dir}/kubeconfig" # its own file: ~/.kube/config is never touched
  lab        = try(local.platform.clusters[var.cluster].role, "") == "lab"
}

resource "kind_cluster" "this" {
  name            = "slo-${var.cluster}"
  node_image      = var.node_image
  kubeconfig_path = local.kubeconfig
  wait_for_ready  = true
  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"
    node {
      role = "control-plane"
      # the lab guard: fault experiments refuse to run unless every node carries this label
      labels = local.lab ? { "slo-platform/lab" = "true" } : {}
    }
  }

  lifecycle {
    precondition {
      condition     = try(local.platform.clusters[var.cluster].runtime, "") == "kind"
      error_message = "platform.yaml: clusters.${var.cluster}.runtime must be kind for this root (use bootstrap/tofu/cluster otherwise)."
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
}

provider "helm" {
  kubernetes = {
    host                   = kind_cluster.this.endpoint
    cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
    client_certificate     = kind_cluster.this.client_certificate
    client_key             = kind_cluster.this.client_key
  }
}

module "platform" {
  source               = "../modules/platform"
  platform             = local.platform
  cluster              = var.cluster
  secrets              = try(yamldecode(data.sops_file.secrets[0].raw), {})
  git_token            = var.git_token
  instance_dir         = var.instance_dir
  argocd_chart_version = var.argocd_chart_version
  depends_on           = [kind_cluster.this]
}

output "kubeconfig" {
  value = local.kubeconfig
}

output "next" {
  value = <<-EOT
    export KUBECONFIG=${local.kubeconfig}
    kubectl -n argocd get applications              # slo-platform first, then the platform's Applications
    ${module.platform.argocd_ui}
      password: ${module.platform.argocd_admin_password_file}
    Grafana password: ${module.platform.grafana_admin_password_file}
  EOT
}
