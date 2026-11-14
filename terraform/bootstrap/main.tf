# One-time setup for GitOps. Run this once, by hand, with admin credentials:
#   cd terraform/bootstrap
#   terraform init
#   terraform apply -var github_repository=<owner>/<repo>
#
# It creates what the pipeline needs before it can manage anything else:
#   - an S3 bucket for Terraform state (versioned, encrypted, TLS only)
#   - GitHub's OIDC identity provider, so workflows get short-lived AWS
#     credentials and no access keys are stored anywhere
#   - a read-only role for plans (pull requests and the daily drift check)
#   - a deploy role for apply/destroy and image pushes, assumable only from
#     the GitHub "production" environment
#
# Everything else long-lived (ECR, team IAM) is in terraform/shared, which the
# pipeline applies, so it changes through pull requests like the rest.
#
# This module's own state stays local (terraform/bootstrap/terraform.tfstate,
# ignored by git). It holds no secrets; keep a copy somewhere safe.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = var.project, ManagedBy = "terraform-bootstrap" } }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account     = data.aws_caller_identity.current.account_id
  oidc_host   = "token.actions.githubusercontent.com"
  oidc_arn    = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${data.aws_partition.current.partition}:iam::${local.account}:oidc-provider/${local.oidc_host}"
  repo        = var.github_subject_prefix != "" ? var.github_subject_prefix : "repo:${var.github_repository}"
  environment = "production"
}

# ---------------------------------------------------------------- state bucket

resource "aws_s3_bucket" "state" {
  bucket = "${var.project}-tfstate-${local.account}-${var.region}"

  lifecycle {
    prevent_destroy = true # losing state orphans every resource it tracks
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

data "aws_iam_policy_document" "state_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_bucket.json
}

# ---------------------------------------------------------------- GitHub OIDC

# An account can have only one provider for this URL. If yours already has
# one, set create_oidc_provider = false and it is reused.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's certificate itself; the thumbprint is still a required field.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Pull requests (plan) and the main branch (scheduled drift check).
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["${local.repo}:pull_request", "${local.repo}:ref:refs/heads/main"]
    }
  }
}

data "aws_iam_policy_document" "trust_deploy" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only jobs running in the "production" environment. Restrict that
    # environment to the main branch in GitHub, or any branch could deploy.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["${local.repo}:environment:${local.environment}"]
    }
  }
}

# ---------------------------------------------------------------- plan role (read only)

resource "aws_iam_role" "plan" {
  name                 = "${var.project}-github-plan"
  description          = "GitHub Actions: terraform plan for pull requests and drift checks. Read only."
  assume_role_policy   = data.aws_iam_policy_document.trust_plan.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "plan_extra" {
  # Plans run with -lock=false, so reading state is enough.
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/${var.project}/*"] # stack and shared state
  }
  statement {
    sid       = "ListState"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  # Refreshing the HMAC signing secret reads its value. ReadOnlyAccess does not include this.
  statement {
    sid       = "RefreshAppSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${local.account}:secret:${var.project}/*"]
  }
}

resource "aws_iam_role_policy" "plan_extra" {
  role   = aws_iam_role.plan.id
  name   = "state-and-secrets"
  policy = data.aws_iam_policy_document.plan_extra.json
}

# ---------------------------------------------------------------- deploy role

# The stack creates IAM roles, VPCs, CloudFront, WAF and more across two
# regions, so the role that applies it is effectively an administrator. The
# guard is who can assume it: the production environment, on main, after review.
resource "aws_iam_role" "deploy" {
  name                 = "${var.project}-github-deploy"
  description          = "GitHub Actions: terraform apply/destroy from the production environment."
  assume_role_policy   = data.aws_iam_policy_document.trust_deploy.json
  max_session_duration = 7200 # first apply waits on CloudFront and RDS
}

resource "aws_iam_role_policy_attachment" "deploy_admin" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AdministratorAccess"
}
