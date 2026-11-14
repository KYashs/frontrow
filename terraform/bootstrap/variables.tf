variable "github_repository" {
  description = "GitHub repository that deploys this stack, as owner/name."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the form owner/name, for example octocat/frontrow."
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
