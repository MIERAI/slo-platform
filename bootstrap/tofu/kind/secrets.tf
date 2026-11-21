# platform.yaml and the (optional) SOPS secrets file of the instance. Decryption: sops' usual key lookup
# (SOPS_AGE_KEY_FILE, ~/.config/sops/age/keys.txt, …).
locals {
  platform     = yamldecode(file("${var.instance_dir}/platform.yaml"))
  secrets_file = var.secrets_file != "" ? var.secrets_file : "${var.instance_dir}/secrets/${var.cluster}.sops.yaml"
  secrets_dir  = "${var.instance_dir}/.secrets/${var.cluster}"
}

data "sops_file" "secrets" {
  count       = fileexists(local.secrets_file) ? 1 : 0
  source_file = local.secrets_file
}
