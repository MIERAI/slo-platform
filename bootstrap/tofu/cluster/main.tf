# The platform on an existing cluster (k3s, a cloud cluster, …), addressed by an explicit kubeconfig context.
#   export TF_VAR_instance_dir=/abs/path/to/instance TF_VAR_cluster=prod TF_VAR_context=my-prod
#   tofu -chdir=bootstrap/tofu/cluster init && tofu -chdir=bootstrap/tofu/cluster apply   (bootstrap/README.md)

variable "kubeconfig" {
  description = "kubeconfig file"
  type        = string
  default     = "~/.kube/config"
}

variable "context" {
  description = "kubeconfig context of the cluster (required: the current context is never used implicitly)"
  type        = string
  validation {
    condition     = var.context != ""
    error_message = "context must name the cluster's kubeconfig context."
  }
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig)
  config_context = var.context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig)
    config_context = var.context
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
}

# The lab guard: experiments refuse to start unless every node is labelled slo-platform/lab=true. On an existing
# cluster labelling is yours (bootstrap/README.md); this check only warns.
check "lab_nodes_labelled" {
  data "kubernetes_nodes" "all" {}
  assert {
    condition = module.platform.role != "lab" || alltrue([
      for n in data.kubernetes_nodes.all.nodes : try(n.metadata[0].labels["slo-platform/lab"], "") == "true"
    ])
    error_message = "Role lab, but not every node is labelled: kubectl --context ${var.context} label nodes --all slo-platform/lab=true"
  }
}

output "next" {
  value = <<-EOT
    kubectl --context ${var.context} -n argocd get applications     # slo-platform first, then the platform's Applications
    kubectl --context ${var.context} ${trimprefix(module.platform.argocd_ui, "kubectl ")}
      password: ${module.platform.argocd_admin_password_file}
    Grafana password: ${module.platform.grafana_admin_password_file}
  EOT
}
