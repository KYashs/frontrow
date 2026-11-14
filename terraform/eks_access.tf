# Who can use the cluster, through EKS access entries (no aws-auth ConfigMap).
# Every principal is an IAM role: the pipeline's roles (terraform/bootstrap) and
# the team roles people assume with MFA (terraform/shared).
#
#   frontrow-github-deploy  cluster admin   installs Argo CD (argocd.tf)
#   frontrow-github-plan    admin view      reads Helm release state during plans
#   frontrow-admin          cluster admin
#   frontrow-developer      edit in the frontrow namespace, view everywhere
#   frontrow-readonly       view everywhere (no Secrets)

data "aws_iam_role" "access" {
  for_each = toset(["github-deploy", "github-plan", "admin", "developer", "readonly"])
  name     = "${var.project}-${each.key}"
}

locals {
  cluster_access = {
    github-deploy = [{ policy = "AmazonEKSClusterAdminPolicy", namespaces = [] }]
    github-plan   = [{ policy = "AmazonEKSAdminViewPolicy", namespaces = [] }]
    admin         = [{ policy = "AmazonEKSClusterAdminPolicy", namespaces = [] }]
    developer = [
      { policy = "AmazonEKSEditPolicy", namespaces = ["frontrow"] },
      { policy = "AmazonEKSViewPolicy", namespaces = [] },
    ]
    readonly = [{ policy = "AmazonEKSViewPolicy", namespaces = [] }]
  }

  cluster_access_policies = merge([
    for who, grants in local.cluster_access : {
      for g in grants : "${who}/${g.policy}" => merge(g, { who = who })
    }
  ]...)
}

resource "aws_eks_access_entry" "this" {
  for_each      = local.cluster_access
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = data.aws_iam_role.access[each.key].arn
}

resource "aws_eks_access_policy_association" "this" {
  for_each      = local.cluster_access_policies
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = data.aws_iam_role.access[each.value.who].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/${each.value.policy}"

  access_scope {
    type       = length(each.value.namespaces) > 0 ? "namespace" : "cluster"
    namespaces = each.value.namespaces
  }

  depends_on = [aws_eks_access_entry.this]
}
