# Production configuration. This file is the source of truth: change it in a
# pull request, and the pipeline applies it on merge.
#
# Personal addresses are not kept in git. The pipeline passes them in from
# GitHub repository variables:
#   SENDER_EMAIL -> var.sender_email   (required, SES sender)
#   ALERT_EMAIL  -> var.alert_email    (optional, CloudWatch alarms)
#
# The pipeline also sets api_image_tag (a hash of the API's source, see
# scripts/image-tag.sh) and gitops_repo_url (this repository, for Argo CD).

region = "us-east-1"

eks_version = "1.37" # standard support until 2027-12-01
# m7i-flex.large: 2 vCPU / 8 GiB like t3.large, and allowed on the AWS Free plan.
node_instance_type = "m7i-flex.large"
node_min           = 2
node_max           = 4
db_multi_az        = false

waf_enabled          = true
waf_api_rate_limit   = 3000
waf_holds_rate_limit = 300

hold_seconds = 480

# Optional custom domain (needs a Route 53 hosted zone you own):
# domain_name    = "tickets.example.com"
# hosted_zone_id = "Z0123456789ABCDEFGHIJ"

# Firehose needs a paid AWS plan. Set true after upgrading to turn the order data lake on.
analytics_enabled = false

# AWS hasn't verified this account for CloudFront yet, so the ALB is the public
# entry point (HTTP) and the API pods serve the site. Set true once verified.
cloudfront_enabled = false

# Your own IAM user, so kubectl (Grafana, Argo CD port-forwards) works from your laptop.
cluster_admin_arns = ["arn:aws:iam::859928916413:user/kyash-admin"]
