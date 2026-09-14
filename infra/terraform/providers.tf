terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Region is hardcoded (not a variable) — this project manages a single
# environment only (see design.md decision 1). Credentials come from the
# default AWS credential chain (~/.aws/credentials), never hardcoded here.
provider "aws" {
  region = "ap-northeast-3"
}
