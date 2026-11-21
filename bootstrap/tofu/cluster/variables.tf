variable "instance_dir" {
  description = "Absolute path of your instance repository checkout (holds platform.yaml)"
  type        = string
  validation {
    condition     = startswith(var.instance_dir, "/")
    error_message = "instance_dir must be an absolute path (tofu -chdir resolves relative paths against this directory), e.g. TF_VAR_instance_dir=$PWD."
  }
}

variable "cluster" {
  description = "The cluster to bootstrap: a key of platform.clusters"
  type        = string
}

variable "secrets_file" {
  description = "SOPS-encrypted secrets file; \"\" = <instance_dir>/secrets/<cluster>.sops.yaml. Missing file = throwaway values"
  type        = string
  default     = ""
}

variable "git_token" {
  description = "Overrides git.token of the secrets file, e.g. TF_VAR_git_token=$(gh auth token)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "argocd_chart_version" {
  description = "argo-cd Helm chart version (null = the module's pinned default)"
  type        = string
  default     = null
}
