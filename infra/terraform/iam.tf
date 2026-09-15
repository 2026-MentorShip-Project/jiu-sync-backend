# IAM Role for the EC2 instance to assume, enabling Session Manager (SSM)
# access without any SSH key pair (design.md decision 3: instance identity
# via IAM Role + instance profile is distinct from the operator's IAM User
# credentials used to run Terraform itself).

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2_ssm" {
  name               = "jiu-sync-backend-ec2-ssm"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

# AWS-managed policy granting the permissions the SSM Agent needs to
# register the instance and accept Session Manager connections.
resource "aws_iam_role_policy_attachment" "ec2_ssm_core" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Instance profile wrapping the role above — this is what gets attached to
# the aws_instance resource in a later task (4.1).
resource "aws_iam_instance_profile" "ec2_ssm" {
  name = "jiu-sync-backend-ec2-ssm"
  role = aws_iam_role.ec2_ssm.name
}
