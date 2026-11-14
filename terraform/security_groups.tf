# Step 10 - security groups: the rules on each component's door.
#   Internet (CloudFront only) -> ALB :80 -> API pods :8000 -> RDS :5432
# Pods use the EKS cluster security group (VPC CNI gives them node-subnet IPs),
# so the rules below reference it.

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  cluster_sg = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "ALB: HTTP from CloudFront origin-facing IPs only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.project}-alb" }
}

resource "aws_security_group" "rds" {
  name        = "${var.project}-rds"
  description = "RDS: Postgres from the EKS cluster only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.project}-rds" }
}

# ---- ALB ----
resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront edge only"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_pods" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to API pods"
  referenced_security_group_id = local.cluster_sg
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# ---- EKS cluster (nodes and pods) ----
resource "aws_vpc_security_group_ingress_rule" "pods_from_alb" {
  security_group_id            = local.cluster_sg
  description                  = "API port from the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# ---- RDS ----
resource "aws_vpc_security_group_ingress_rule" "rds_from_pods" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the EKS cluster"
  referenced_security_group_id = local.cluster_sg
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
