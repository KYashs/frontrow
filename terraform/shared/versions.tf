# Long-lived resources shared across deploys: they survive the Destroy workflow.
#   ECR repository for the API image
#   Team IAM: groups, MFA-protected roles, and users
# The pipeline applies this before the main stack (.github/workflows/deploy.yml).

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  backend "s3" {
    key          = "frontrow/shared.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = var.project, ManagedBy = "terraform", Lifecycle = "shared" } }
}

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
}
