terraform {
  required_version = ">= 1.10"
  required_providers {
    helm       = { source = "hashicorp/helm", version = "~> 3.1" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38" }
    local      = { source = "hashicorp/local", version = "~> 2.5" }
    random     = { source = "hashicorp/random", version = "~> 3.7" }
  }
}
