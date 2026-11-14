output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "team_roles" {
  description = "Assume with MFA, e.g. aws sts assume-role --role-arn <arn> --serial-number <mfa-arn> --token-code <code>."
  value       = { for k, r in aws_iam_role.team : k => r.arn }
}

output "switch_role_urls" {
  description = "Console links for switching into each role."
  value       = { for k, r in aws_iam_role.team : k => "https://signin.aws.amazon.com/switchrole?account=${local.account}&roleName=${r.name}&displayName=${r.name}" }
}
