# EKS runs the API (ADR-10). AWS runs the control plane; the nodes are a managed
# node group in the private app subnets, one per AZ to start.

# ---------------------------------------------------------------- control plane
data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_cluster" {
  name               = "${var.project}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Created first so EKS writes control-plane logs into a group with a retention.
resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${var.project}/cluster"
  retention_in_days = 14
}

resource "aws_eks_cluster" "main" {
  name     = var.project
  version  = var.eks_version
  role_arn = aws_iam_role.eks_cluster.arn

  # Who did what in the cluster, and who authenticated: in CloudWatch Logs.
  enabled_cluster_log_types = ["audit", "authenticator"]

  vpc_config {
    subnet_ids              = aws_subnet.private_app[*].id
    endpoint_private_access = true
    endpoint_public_access  = true # for the pipeline and kubectl; every call is still IAM-authenticated
    public_access_cidrs     = var.eks_public_access_cidrs
  }

  # Access is granted only through access entries (eks_access.tf), never the aws-auth ConfigMap.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  # When standard support ends, upgrade instead of silently paying for extended support.
  upgrade_policy {
    support_type = "STANDARD"
  }

  depends_on = [aws_iam_role_policy_attachment.eks_cluster, aws_cloudwatch_log_group.eks]
}

# ---------------------------------------------------------------- nodes
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.project}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Only what the node itself needs. Pods get their own roles (eks_iam.tf), and
# the launch template below stops them from borrowing this one.
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryPullOnly",
    "AmazonSSMManagedInstanceCore", # Session Manager onto nodes: no SSH, no bastion
  ])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

resource "aws_launch_template" "node" {
  name_prefix            = "${var.project}-node-"
  update_default_version = true

  # IMDSv2 with a hop limit of 1: pods can't reach the instance metadata
  # service, so they can't use the node's IAM role. They use pod identity instead.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.project}-eks-node" }
  }
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.project}-default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private_app[*].id
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  instance_types  = [var.node_instance_type]

  scaling_config {
    min_size     = var.node_min
    desired_size = var.node_min
    max_size     = var.node_max
  }

  update_config {
    max_unavailable = 1 # the API's PodDisruptionBudget keeps one pod serving
  }

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.before_nodes,
    aws_route_table_association.private_app,
    aws_vpc_endpoint.s3,
  ]
}

# ---------------------------------------------------------------- add-ons
# Networking and pod identity first: nodes need them to become Ready.
resource "aws_eks_addon" "before_nodes" {
  for_each = toset(["vpc-cni", "kube-proxy", "eks-pod-identity-agent"])

  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = each.key
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

# These run as Deployments, so they need nodes.
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.main]
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.main, aws_eks_pod_identity_association.this]
}
