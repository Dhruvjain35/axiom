# AXIOM :: CloudWatch log groups.
#
# Two groups, not one. When a worker is SIGKILLed mid-flight you want to read
# the worker stream without the API's request log interleaved into it, and
# `aws logs tail /axiom/worker --follow` is the command that shows a judge the
# recovery happening in real time.

resource "aws_cloudwatch_log_group" "api" {
  name              = "/${var.name_prefix}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/${var.name_prefix}/worker"
  retention_in_days = var.log_retention_days
}

# ECS Exec session transcripts. Separate group with the same retention, so the
# on-camera `kill -9` is auditable afterwards and does not pollute the worker's
# own stream.
resource "aws_cloudwatch_log_group" "exec" {
  count             = var.enable_execute_command ? 1 : 0
  name              = "/${var.name_prefix}/exec"
  retention_in_days = var.log_retention_days
}
