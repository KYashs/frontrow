output "site_url" {
  description = "Open this in your browser."
  value       = local.use_domain ? "https://${var.domain_name}" : var.cloudfront_enabled ? "https://${aws_cloudfront_distribution.main[0].domain_name}" : "http://${aws_lb.app.dns_name}"
}

output "websocket_url" {
  value = aws_apigatewayv2_stage.live.invoke_url
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.onsale.dashboard_name}"
}

output "athena_console_url" {
  value = "https://${var.region}.console.aws.amazon.com/athena/home?region=${var.region}#/query-editor"
}

output "xray_service_map_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#xray:service-map/map"
}

output "region" {
  value = var.region
}

output "tables" {
  value = local.table_env
}

output "seed_result" {
  value = jsondecode(aws_lambda_invocation.seed_inventory.result)
}

output "alb_dns_name" {
  description = "Not reachable directly: its security group only admits CloudFront."
  value       = aws_lb.app.dns_name
}

output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "kubeconfig_command" {
  description = "Point kubectl at the cluster (after assuming a frontrow-* team role)."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${aws_eks_cluster.main.name}"
}

output "grafana" {
  description = "Grafana is not exposed publicly. Port-forward, then log in as admin."
  value = {
    port_forward = "kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80"
    url          = "http://localhost:3000"
    password     = "kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d"
  }
}

output "argocd" {
  description = "Argo CD UI, also port-forward only."
  value = {
    port_forward = "kubectl -n argocd port-forward svc/argocd-server 8080:80"
    url          = "http://localhost:8080"
    password     = "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
  }
}

output "api_image" {
  value = var.api_image_tag == "" ? "not deployed (api_image_tag is empty)" : "${data.aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
}
