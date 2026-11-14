variable "project" {
  type    = string
  default = "frontrow"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "team_members" {
  description = "IAM user name => group (admins, developers or readonly). Kept in git: terraform/environments/shared.tfvars."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for g in values(var.team_members) : contains(["admins", "developers", "readonly"], g)])
    error_message = "Each member's group must be admins, developers or readonly."
  }
}
