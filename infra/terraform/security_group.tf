# Security Group for the deployment EC2 instance. Inbound is limited to
# HTTP/HTTPS only (design.md — 對外網路暴露面僅限 HTTP 與 HTTPS); SSH (22) is
# intentionally not opened since instance access goes through SSM Session
# Manager instead (see iam.tf).
resource "aws_security_group" "ec2_web" {
  name        = "jiu-sync-backend-ec2-web"
  description = "Allow inbound HTTP/HTTPS only; all egress. No SSH."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "jiu-sync-backend-ec2-web"
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.ec2_web.id
  description       = "HTTP from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.ec2_web.id
  description       = "HTTPS from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Egress all — SSM, apt, and docker pull all need outbound access
# (design.md — this is the standard approach for this scenario).
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.ec2_web.id
  description       = "Allow all outbound"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
