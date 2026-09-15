# t2.micro is not offered in every AZ of ap-northeast-3 (confirmed via
# `aws ec2 describe-instance-type-offerings` — only ap-northeast-3a supports
# it; a plain `element(data.aws_subnets.default.ids, 0)` can land on a subnet
# in 3b/3c and fail RunInstances). Restrict subnet selection to a subnet in
# an AZ that actually offers the chosen instance type.
data "aws_ec2_instance_type_offerings" "t2_micro" {
  filter {
    name   = "instance-type"
    values = ["t2.micro"]
  }

  location_type = "availability-zone"
}

data "aws_subnets" "instance_az" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.t2_micro.locations
  }
}

# Deployment EC2 instance. Sized as t2.micro (design.md — no known workload
# requirement yet beyond "try it and observe"; a known, accepted risk if it
# turns out too small for Django+Postgres+Redis+Nginx later).
#
# Access is via SSM only (see iam.tf) — no key_name is set, so no SSH key
# pair is associated with this instance.
resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t2.micro"
  subnet_id              = element(data.aws_subnets.instance_az.ids, 0)
  vpc_security_group_ids = [aws_security_group.ec2_web.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_ssm.name

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
  }

  tags = {
    Name = "jiu-sync-backend"
  }
}
