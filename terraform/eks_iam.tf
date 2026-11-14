# EKS Pod Identity: each workload's service account maps to its own IAM role.
# No node-wide credentials, no long-lived keys, nothing annotated in the charts.
# The service account names must match deploy/.

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------- the API
# Exactly what the code calls, on exactly the resources it touches.
data "aws_iam_policy_document" "api" {
  statement {
    sid       = "ReadInventory"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem"]
    resources = [local.t.seats, local.t.inventory, local.t.holds, local.t.orders, local.t.waiting]
  }
  statement {
    sid       = "HoldsCheckoutAndQueue" # TransactWriteItems is authorised per item
    actions   = ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem"]
    resources = [local.t.seats, local.t.inventory, local.t.holds, local.t.waiting]
  }
  statement {
    sid       = "DeepHealth"
    actions   = ["dynamodb:DescribeTable"]
    resources = [local.t.seats]
  }
  statement {
    sid       = "SendCheckouts"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.checkout.arn]
  }
  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn, aws_secretsmanager_secret.token_key.arn]
  }
}

# ---------------------------------------------------------------- load balancer controller
# Only registers pod IPs in the one target group Terraform owns. It never
# creates load balancers or edits security groups, so it needs far less than
# the controller's usual policy.
data "aws_iam_policy_document" "lb_controller" {
  statement {
    sid = "Discover"
    actions = [
      "elasticloadbalancing:DescribeLoadBalancers",
      "elasticloadbalancing:DescribeListeners",
      "elasticloadbalancing:DescribeRules",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetGroupAttributes",
      "elasticloadbalancing:DescribeTargetHealth",
      "elasticloadbalancing:DescribeTags",
      "ec2:DescribeVpcs",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeInstances",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeAvailabilityZones",
    ]
    resources = ["*"] # Describe calls don't support resource-level permissions
  }
  statement {
    sid       = "RegisterApiPods"
    actions   = ["elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets"]
    resources = [aws_lb_target_group.app.arn]
  }
}

# ---------------------------------------------------------------- Loki
data "aws_iam_policy_document" "loki" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.logs.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.logs.arn}/*"]
  }
}

# ---------------------------------------------------------------- Grafana
# Read CloudWatch metrics and logs, so one Grafana shows the pods (Prometheus)
# next to the Lambdas, SQS and DynamoDB (CloudWatch).
data "aws_iam_policy_document" "grafana" {
  statement {
    actions = [
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DescribeAlarmsForMetric",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "logs:DescribeLogGroups",
      "logs:GetLogGroupFields",
      "logs:StartQuery",
      "logs:StopQuery",
      "logs:GetQueryResults",
      "logs:GetLogEvents",
      "ec2:DescribeRegions",
      "tag:GetResources",
    ]
    resources = ["*"]
  }
}

locals {
  pod_roles = {
    api           = { namespace = "frontrow", service_account = "frontrow-api", policy = data.aws_iam_policy_document.api.json }
    lb-controller = { namespace = "kube-system", service_account = "aws-load-balancer-controller", policy = data.aws_iam_policy_document.lb_controller.json }
    loki          = { namespace = "monitoring", service_account = "loki", policy = data.aws_iam_policy_document.loki.json }
    grafana       = { namespace = "monitoring", service_account = "grafana", policy = data.aws_iam_policy_document.grafana.json }
    ebs-csi       = { namespace = "kube-system", service_account = "ebs-csi-controller-sa", policy = null }
  }
}

resource "aws_iam_role" "pod" {
  for_each           = local.pod_roles
  name               = "${var.project}-pod-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
}

resource "aws_iam_role_policy" "pod" {
  # Filter on the key, not on the policy JSON: the JSON is unknown until apply,
  # and for_each keys must be known at plan time. ebs-csi uses a managed policy.
  for_each = { for k, v in local.pod_roles : k => v if k != "ebs-csi" }
  name     = "least-privilege"
  role     = aws_iam_role.pod[each.key].id
  policy   = each.value.policy
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.pod["ebs-csi"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_pod_identity_association" "this" {
  for_each        = local.pod_roles
  cluster_name    = aws_eks_cluster.main.name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.pod[each.key].arn

  depends_on = [aws_eks_addon.before_nodes]
}
