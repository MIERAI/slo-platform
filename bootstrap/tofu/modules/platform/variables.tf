variable "platform" {
  description = "The instance's platform.yaml, decoded (apiVersion slo-platform/v1)"
  type        = any
}

variable "cluster" {
  description = "Name of the cluster being bootstrapped: a key of platform.clusters"
  type        = string
}

variable "secrets" {
  description = "The decrypted secrets/<cluster>.sops.yaml (git.token, grafana.adminPassword, secrets.<namespace>.<name>.<key>); {} or null = none"
  type        = any
  sensitive   = true
  default     = {}
}

variable "git_token" {
  description = "Overrides secrets git.token when not empty (e.g. TF_VAR_git_token=$(gh auth token))"
  type        = string
  sensitive   = true
  default     = ""
}

variable "instance_dir" {
  description = "Absolute path of the instance repository checkout; passwords are written to <instance_dir>/.secrets/<cluster>/"
  type        = string
}

variable "argocd_chart_version" {
  description = "argo-cd Helm chart (https://argoproj.github.io/argo-helm)"
  type        = string
  default     = "10.18.2" # Argo CD v3.6.2
  nullable    = false
}

variable "argocd_apps_chart_version" {
  description = "argocd-apps Helm chart: creates the root Application once"
  type        = string
  default     = "2.0.8"
}

variable "labels" {
  description = "Extra labels on the namespaces and Secrets created here"
  type        = map(string)
  default     = {}
}
