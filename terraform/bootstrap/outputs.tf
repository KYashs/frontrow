output "github_variables" {
  description = "Set these as GitHub repository variables (Settings > Secrets and variables > Actions > Variables)."
  value = {
    AWS_PLAN_ROLE_ARN   = aws_iam_role.plan.arn
    AWS_DEPLOY_ROLE_ARN = aws_iam_role.deploy.arn
    TF_STATE_BUCKET     = aws_s3_bucket.state.bucket
    TF_STATE_REGION     = var.region
  }
}
