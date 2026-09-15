# Default VPC for ap-northeast-3 — this project does not create its own
# network resources (design.md decision 1).
data "aws_vpc" "default" {
  default = true
}

# Subnets belonging to the default VPC above.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Latest official Canonical Ubuntu 24.04 LTS (Noble Numbat) amd64 server AMI.
# Owner ID 099720109477 is Canonical's official AWS account — filtering on it
# avoids picking up unofficial/community AMIs with a matching name.
#
# NOTE: the name pattern must match the *full* AMI name Canonical publishes,
# which is prefixed with "ubuntu/images/hvm-ssd-gp3/" — a bare
# "ubuntu-noble-24.04-amd64-server-*" pattern does not match anything in
# ap-northeast-3 (verified via `aws ec2 describe-images`), since the AWS name
# filter requires matching the entire string, not a substring.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}
