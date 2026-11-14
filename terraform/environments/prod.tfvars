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

region = "ap-south-1"

eks_version        = "1.37" # standard support until 2027-12-01
node_instance_type = "t3.large"
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
