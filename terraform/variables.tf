variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "frontrow"
}

variable "region" {
  description = "Region for the regional services."
  type        = string
  default     = "us-east-1" # N. Virginia: lowest prices
}

variable "sender_email" {
  description = "Address SES sends ticket confirmations from. SES emails a verification link on apply."
  type        = string
  sensitive   = true # kept out of public plan output and PR comments
}

variable "alert_email" {
  description = "Optional address for CloudWatch alarm notifications."
  type        = string
  default     = ""
  sensitive   = true
}

variable "domain_name" {
  description = "Optional custom domain (e.g. tickets.example.com). Empty = use the CloudFront domain."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone that owns domain_name."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "eks_version" {
  description = "Kubernetes version. Keep it in EKS standard support: extended support costs 6x for the control plane."
  type        = string
  default     = "1.37"
}

variable "eks_public_access_cidrs" {
  description = "Who can reach the EKS API endpoint (still IAM-authenticated). GitHub-hosted runners need it, and their IPs vary."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "node_instance_type" {
  type    = string
  default = "t3.large" # 8 GiB: room for Prometheus, Loki, Argo CD and the API
}

variable "node_min" {
  type    = number
  default = 2 # one per AZ
}

variable "node_max" {
  type    = number
  default = 4
}

variable "api_image_tag" {
  description = "API image tag in ECR (scripts/image-tag.sh). The pipeline sets it; empty skips deploying the API."
  type        = string
  default     = ""
}

variable "gitops_repo_url" {
  description = "Git repository Argo CD syncs deploy/ from. The pipeline sets it to this repository."
  type        = string
  default     = "https://github.com/example/frontrow.git"
}

variable "gitops_revision" {
  description = "Branch Argo CD tracks."
  type        = string
  default     = "main"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "Standby RDS replica in a second AZ. Doubles RDS cost; turn on to demo failover."
  type        = bool
  default     = false
}

variable "waf_enabled" {
  type    = bool
  default = true
}

variable "waf_api_rate_limit" {
  description = "Max /api requests per IP per 5 minutes before WAF blocks. Raise it for single-machine load tests."
  type        = number
  default     = 3000
}

variable "waf_holds_rate_limit" {
  description = "Max hold attempts per IP per 5 minutes (anti-bot)."
  type        = number
  default     = 300
}

variable "hold_seconds" {
  description = "How long a hold lasts before seats go back on sale."
  type        = number
  default     = 480
}

variable "analytics_enabled" {
  description = "Stream every order through Firehose into the S3 data lake. AWS Free plan accounts can't use Firehose; set false there."
  type        = bool
  default     = true
}

variable "cloudfront_enabled" {
  description = <<-EOT
    true: CloudFront serves the site from S3 and proxies /api/* to the ALB, which
    only accepts CloudFront. false: no CDN - the ALB is public over HTTP and the
    API pods also serve the site. For accounts AWS hasn't verified for CloudFront.
  EOT
  type        = bool
  default     = true
}

variable "cluster_admin_arns" {
  description = "Extra IAM users or roles to make EKS cluster admins (for kubectl), besides the pipeline and team roles."
  type        = list(string)
  default     = []
}
