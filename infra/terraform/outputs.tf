output "instance_id" {
  description = "ID of the deployment EC2 instance."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Stable public IP address (Elastic IP) of the deployment instance."
  value       = aws_eip.app.public_ip
}
