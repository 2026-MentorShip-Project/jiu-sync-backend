# Elastic IP directly associated to the instance via the `instance`
# attribute — no separate aws_eip_association resource, since there is only
# ever a single instance to associate it with (design.md decision 4).
# Keeps the instance's public IP stable across stop/start cycles.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = "jiu-sync-backend"
  }
}
