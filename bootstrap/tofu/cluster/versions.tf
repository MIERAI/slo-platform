terraform {
  required_version = ">= 1.10"
  # State: local, inside the instance directory (gitignored by init). It holds every secret in plain text: the git
  # token, the passwords, the applications' Secrets — treat the file like the age key (bootstrap/README.md).
  # Early variable evaluation (OpenTofu ≥ 1.8): TF_VAR_instance_dir and TF_VAR_cluster must be set for init as well.
  backend "local" {
    path = "${var.instance_dir}/.tofu/${var.cluster}.tfstate"
  }
  required_providers {
    helm       = { source = "hashicorp/helm", version = "~> 3.1" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38" }
    local      = { source = "hashicorp/local", version = "~> 2.5" }
    random     = { source = "hashicorp/random", version = "~> 3.7" }
    sops       = { source = "carlpett/sops", version = "~> 1.3" }
  }
}
