# AXIOM :: provider requirements.
#
# No remote state backend is declared. This module manages one demo environment
# from one laptop for four weeks; an S3 + DynamoDB state backend would be two
# more billable resources and a lock nobody is contending. If a second person
# ever runs it, add a `backend "s3"` block here and migrate — that is the moment
# it becomes worth the cost, and not before.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource carries these. When the demo is torn down after judging on
  # Sep 15, `Project = axiom` is how you find the stragglers that are still
  # charging you.
  default_tags {
    tags = {
      Project   = "axiom"
      ManagedBy = "terraform"
      Purpose   = "cockroachdb-aws-hackathon-demo"
    }
  }
}

data "aws_caller_identity" "current" {}
