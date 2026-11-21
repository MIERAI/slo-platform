output "argocd_admin_password_file" {
  description = "Argo CD UI password (user admin), 0600"
  value       = local_sensitive_file.argocd_admin.filename
}

output "grafana_admin_password_file" {
  description = "Grafana password (user admin), 0600"
  value       = local_sensitive_file.grafana_admin.filename
}

output "argocd_ui" {
  description = "Port-forward to the Argo CD UI (prefix with the cluster's kubectl flags)"
  value       = "kubectl -n argocd port-forward svc/argocd-server 8080:80   # then http://localhost:8080, user admin"
}

output "role" {
  description = "The cluster's role from platform.yaml (prod | lab)"
  value       = local.role
}
