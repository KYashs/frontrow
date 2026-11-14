# Team access (ADR-16). People never get permissions directly:
#   user -> group -> may assume one role -> role has the permissions
# Assuming a role requires MFA, and the role is what EKS access entries trust
# (terraform/eks_access.tf), so one identity covers the console, the CLI and kubectl.
#
#   group              role                 AWS                          Kubernetes
#   frontrow-admins     frontrow-admin       AdministratorAccess          cluster admin
#   frontrow-developers frontrow-developer   view + on-sale operations     edit frontrow, view all
#   frontrow-readonly   frontrow-readonly    view (no data)               view all

locals {
  teams = {
    admins     = { role = "admin", session_hours = 1 }
    developers = { role = "developer", session_hours = 4 }
    readonly   = { role = "readonly", session_hours = 4 }
  }
  tables = "arn:aws:dynamodb:${var.region}:${local.account}:table/${var.project}-"
}

# ---------------------------------------------------------------- roles
data "aws_iam_policy_document" "assume_with_mfa" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"] # only principals this account allows (the groups below)
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role" "team" {
  for_each             = local.teams
  name                 = "${var.project}-${each.value.role}"
  description          = "Assumed by the ${var.project}-${each.key} group, with MFA."
  assume_role_policy   = data.aws_iam_policy_document.assume_with_mfa.json
  max_session_duration = each.value.session_hours * 3600
}

resource "aws_iam_role_policy_attachment" "admin" {
  role       = aws_iam_role.team["admins"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ViewOnlyAccess shows configuration and metrics but not data (no S3 objects,
# no DynamoDB items), so order emails stay private.
resource "aws_iam_role_policy_attachment" "view" {
  for_each   = toset(["developers", "readonly"])
  role       = aws_iam_role.team[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
}

# What developers need to run the demo and debug it: scripts/onsale.py,
# scripts/reset_event.py, log searches, seat data (no orders: they hold emails).
data "aws_iam_policy_document" "developer" {
  statement {
    sid       = "RunOnSales"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem"]
    resources = ["${local.tables}waiting-room"]
  }
  statement {
    sid       = "ReadSeatData"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = ["${local.tables}seats", "${local.tables}inventory", "${local.tables}holds", "${local.tables}holds/index/*"]
  }
  statement {
    sid       = "ResetEvents"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${local.account}:function:${var.project}-inventory-seeder"]
  }
  statement {
    sid       = "SearchLogs"
    actions   = ["logs:StartQuery", "logs:GetQueryResults", "logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = ["*"]
  }
  statement {
    sid       = "UseTheCluster"
    actions   = ["eks:DescribeCluster", "eks:ListClusters"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "developer" {
  name   = "operate-frontrow"
  role   = aws_iam_role.team["developers"].id
  policy = data.aws_iam_policy_document.developer.json
}

resource "aws_iam_role_policy" "readonly_eks" {
  name = "describe-cluster" # enough for `aws eks update-kubeconfig`
  role = aws_iam_role.team["readonly"].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["eks:DescribeCluster", "eks:ListClusters"], Resource = "*" }]
  })
}

# ---------------------------------------------------------------- groups
# Users can only manage their own password, MFA device and access keys, and
# assume their group's role. Everything else happens through the role.
data "aws_iam_policy_document" "self_service" {
  statement {
    sid       = "SeeOwnUser"
    actions   = ["iam:GetUser", "iam:ListMFADevices", "iam:ListAccessKeys", "iam:GetLoginProfile"]
    resources = ["arn:aws:iam::${local.account}:user/$${aws:username}"]
  }
  statement {
    sid = "ManageOwnCredentials"
    actions = [
      "iam:ChangePassword",
      "iam:CreateAccessKey", "iam:DeleteAccessKey", "iam:UpdateAccessKey",
      "iam:EnableMFADevice", "iam:ResyncMFADevice", "iam:DeactivateMFADevice",
    ]
    resources = ["arn:aws:iam::${local.account}:user/$${aws:username}"]
  }
  statement {
    sid       = "ManageOwnVirtualMFA"
    actions   = ["iam:CreateVirtualMFADevice", "iam:DeleteVirtualMFADevice"]
    resources = ["arn:aws:iam::${local.account}:mfa/$${aws:username}"]
  }
  statement {
    sid       = "ListMFAForConsole"
    actions   = ["iam:ListVirtualMFADevices", "iam:GetAccountPasswordPolicy"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "self_service" {
  name   = "${var.project}-self-service"
  policy = data.aws_iam_policy_document.self_service.json
}

resource "aws_iam_group" "team" {
  for_each = local.teams
  name     = "${var.project}-${each.key}"
}

resource "aws_iam_group_policy_attachment" "self_service" {
  for_each   = local.teams
  group      = aws_iam_group.team[each.key].name
  policy_arn = aws_iam_policy.self_service.arn
}

resource "aws_iam_group_policy" "assume" {
  for_each = local.teams
  name     = "assume-${each.value.role}"
  group    = aws_iam_group.team[each.key].name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.team[each.key].arn }]
  })
}

# ---------------------------------------------------------------- users
# Created without a password or keys: an admin sets the console password once,
# and the person sets up MFA at first sign-in.
resource "aws_iam_user" "member" {
  for_each      = var.team_members
  name          = each.key
  force_destroy = true # lets Terraform remove a user who set up MFA or keys
}

resource "aws_iam_user_group_membership" "member" {
  for_each = var.team_members
  user     = aws_iam_user.member[each.key].name
  groups   = [aws_iam_group.team[each.value].name]
}
