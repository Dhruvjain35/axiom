# AXIOM :: IAM.
#
# Two roles with different jobs, which is the whole reason ECS has two.
#
#   execution role — held by the ECS AGENT, before the container starts. Pulls
#                    the image, decrypts the SSM parameters, opens the log
#                    stream. It never runs application code.
#   task role      — held by the APPLICATION. Calls Bedrock. It cannot read the
#                    database secret, because by the time the process is running
#                    the secret is already in its environment and a second path
#                    to it is only an extra way to leak it.
#
# The Bedrock statement names exactly two foundation-model ARNs. `bedrock:*` on
# `*` is what most deployments ship; it authorizes invoking every model in the
# account, including ones with different price points and different data-handling
# terms. Two ids, spelled out.

locals {
  ssm_parameter_arns = compact([
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.database_url_parameter_name}",
    var.provider_database_url_parameter_name != ""
    ? "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.provider_database_url_parameter_name}"
    : "",
  ])

  # Foundation-model ARNs carry no account id — the models are AWS's, not yours.
  bedrock_model_arns = concat([
    "arn:aws:bedrock:${var.region}::foundation-model/${var.bedrock_embed_model_id}",
    "arn:aws:bedrock:${var.region}::foundation-model/${var.bedrock_llm_model_id}",
  ], var.extra_bedrock_model_arns)
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy guard: without it, any ECS task in any account that
    # somehow named this role could assume it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# ---------------------------------------------------------- execution role --

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  description        = "ECS agent: pull image, read secrets, write log streams"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# ECR pull + CreateLogStream/PutLogEvents. AWS-managed because it is exactly the
# set the agent needs and AWS updates it when the agent's needs change.
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadAxiomDatabaseSecrets"
    effect    = "Allow"
    actions   = ["ssm:GetParameters"]
    resources = local.ssm_parameter_arns
  }

  # Only for a customer-managed key. The AWS-managed alias/aws/ssm key grants
  # ECS decrypt through its own key policy, so adding kms:Decrypt for it would
  # be a permission that does nothing.
  dynamic "statement" {
    for_each = var.kms_key_arn != "" ? [var.kms_key_arn] : []
    content {
      sid       = "DecryptSecureStringWithCustomerKey"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name_prefix}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --------------------------------------------------------------- task role --

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-ecs-task"
  description        = "AXIOM application: Bedrock on two model ids, its own log group, ECS Exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid    = "InvokeExactlyTheTwoModelsAxiomUses"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_model_arns
  }

  statement {
    sid    = "WriteOwnLogStreams"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.api.arn}:*",
      "${aws_cloudwatch_log_group.worker.arn}:*",
    ]
  }

  # ECS Exec — the on-camera SIGKILL. These four actions do not support
  # resource-level permissions (the channel does not exist until the session
  # opens), so `*` is the only value AWS accepts. Scope is bounded by the fact
  # that only tasks running under this role can assume it, and by
  # enable_execute_command being a variable you can turn off after the demo.
  dynamic "statement" {
    for_each = var.enable_execute_command ? [1] : []
    content {
      sid    = "EcsExecSsmMessagingChannel"
      effect = "Allow"
      actions = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_execute_command ? [1] : []
    content {
      sid       = "EcsExecSessionTranscript"
      effect    = "Allow"
      actions   = ["logs:DescribeLogGroups"]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_execute_command ? [1] : []
    content {
      sid    = "EcsExecSessionTranscriptWrite"
      effect = "Allow"
      actions = [
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
      ]
      resources = ["${aws_cloudwatch_log_group.exec[0].arn}:*"]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name_prefix}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
