variable "github_repository" {
  description = "GitHub repository that deploys this stack, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the form owner/name, for example octocat/frontrow."
  }
}

variable "github_subject_prefix" {
  description = <<-EOT
    Start of the OIDC subject GitHub issues for this repository. Newer repositories
    use immutable IDs (repo:owner@ownerId/name@repoId). Print it with:
    gh api repos/OWNER/NAME/actions/oidc/customization/sub --jq .sub_claim_prefix
    Empty = the classic repo:owner/name form.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.github_subject_prefix == "" || startswith(var.github_subject_prefix, "repo:")
    error_message = "The prefix starts with repo:, for example repo:octocat@583231/frontrow@1296269."
  }
}

variable "project" {
  type    = string
  default = "frontrow"
}

variable "region" {
  description = "Region for the state bucket. Use the same region as the stack."
  type        = string
  default     = "us-east-1"
}

variable "create_oidc_provider" {
  description = "false if this AWS account already has the token.actions.githubusercontent.com provider."
  type        = bool
  default     = true
}
