# AXIOM :: what you need after `terraform apply`.

output "demo_url" {
  description = "The submission's demo URL. Paste this into Devpost."
  value       = var.acm_certificate_arn != "" ? "https://${aws_lb.main.dns_name}" : "http://${aws_lb.main.dns_name}"
}

output "health_url" {
  description = "Smoke test: expect {\"ok\":true,\"db\":true,\"provider\":true,...}"
  value       = "${var.acm_certificate_arn != "" ? "https" : "http"}://${aws_lb.main.dns_name}/api/health"
}

output "alb_dns_name" {
  description = "Stable name for the demo. Point a CNAME at it if you have a domain."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "Push target for scripts/deploy.sh."
  value       = var.create_ecr ? aws_ecr_repository.axiom[0].repository_url : null
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "log_tail_commands" {
  description = "Watch a worker recover a task orphaned by a dead peer, live."
  value = {
    api    = "aws logs tail ${aws_cloudwatch_log_group.api.name} --follow --region ${var.region}"
    worker = "aws logs tail ${aws_cloudwatch_log_group.worker.name} --follow --region ${var.region}"
  }
}

output "kill_a_worker" {
  description = <<-EOT
    The demo. Lists the running worker tasks, then opens a shell in one so you
    can SIGKILL it on camera; ECS replaces the task and AXIOM recovers the lease
    without re-issuing the refund.
  EOT
  value = var.enable_execute_command ? join("\n", [
    "aws ecs list-tasks --cluster ${aws_ecs_cluster.main.name} --service-name ${aws_ecs_service.worker.name} --region ${var.region}",
    "aws ecs execute-command --cluster ${aws_ecs_cluster.main.name} --task <TASK_ARN> --container worker --interactive --command '/bin/sh' --region ${var.region}",
    "# then, inside: kill -9 1",
  ]) : "ECS Exec disabled (enable_execute_command = false)"
}
