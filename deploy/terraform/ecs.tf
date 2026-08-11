# AXIOM :: the cluster, the two task definitions, and the two services.
#
# Fargate, not Lambda, and the reason is not preference. The demo's central
# claim is what happens when a worker is killed MID-FLIGHT — between the
# provider accepting a refund and AXIOM recording that it did. You cannot
# SIGKILL a Lambda invocation. You can `aws ecs execute-command` into a Fargate
# task and `kill -9` it while a judge watches the ledger, which is the difference
# between asserting the crash-window table and demonstrating it.

locals {
  # Set on both containers so the API and the workers cannot disagree about
  # which models exist, which database they talk to, or whether they are online.
  common_environment = [
    { name = "AXIOM_OFFLINE", value = var.axiom_offline ? "1" : "0" },
    { name = "AWS_REGION", value = var.region },
    { name = "AXIOM_EMBED_MODEL", value = var.bedrock_embed_model_id },
    { name = "AXIOM_LLM_MODEL", value = var.bedrock_llm_model_id },
  ]

  common_secrets = concat(
    [{
      name      = "DATABASE_URL"
      valueFrom = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.database_url_parameter_name}"
    }],
    var.provider_database_url_parameter_name != "" ? [{
      name      = "PROVIDER_DATABASE_URL"
      valueFrom = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.provider_database_url_parameter_name}"
    }] : [],
  )

  # A worker has no socket to probe. The only failure worth replacing a task for
  # is "cannot reach the cluster", so the check is a real read-only transaction
  # through the same pool the worker uses.
  worker_health_command = "python -c \"from axiom import db; db.tx(lambda c: c.execute('SELECT 1'), readonly=True)\""
}

resource "aws_ecs_cluster" "main" {
  name = var.name_prefix

  setting {
    # Container Insights is billed per metric per month and would roughly double
    # the observability spend for a demo whose signal is in the logs and in the
    # CockroachDB DB Console.
    name  = "containerInsights"
    value = "disabled"
  }

  dynamic "configuration" {
    for_each = var.enable_execute_command ? [1] : []
    content {
      execute_command_configuration {
        logging = "OVERRIDE"
        log_configuration {
          cloud_watch_log_group_name = aws_cloudwatch_log_group.exec[0].name
        }
      }
    }
  }
}

# ---------------------------------------------------------------- API task --

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = var.image_uri
      essential = true

      # No `command`: the image's CMD already starts uvicorn on 8000.
      portMappings = [
        { containerPort = local.container_port, protocol = "tcp" },
      ]

      environment = local.common_environment
      secrets     = local.common_secrets

      # ECS ignores the Dockerfile's HEALTHCHECK entirely — it must be restated
      # here or the container is never health-checked at the task level.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:${local.container_port}/api/health',timeout=4).read()\""]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }
    },
  ])
}

# ------------------------------------------------------------- worker task --

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = var.image_uri
      essential = true
      command   = ["python", "-m", "axiom.worker"]

      environment = concat(local.common_environment, [
        # ECS injects the task ARN via metadata, but axiom/worker.py takes the
        # ref as an argument or generates one. Left generated: a worker_ref that
        # changes on every restart is CORRECT here, because a restarted worker
        # is a new lease holder and must not inherit the dead one's identity.
      ])
      secrets = local.common_secrets

      healthCheck = {
        command     = ["CMD-SHELL", local.worker_health_command]
        interval    = 30
        timeout     = 10
        retries     = 3
        startPeriod = 20
      }

      # SIGTERM gets 30 seconds to drain. Worth having even though the demo
      # kills with SIGKILL: a graceful ECS deploy should let a worker finish the
      # task it holds rather than force every in-flight step through the
      # recovery path.
      stopTimeout = 30

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "worker"
        }
      }
    },
  ])
}

# ----------------------------------------------------------------- services --

resource "aws_ecs_service" "api" {
  name            = "${var.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets         = local.subnet_ids
    security_groups = [aws_security_group.service.id]
    # Required. Without a public IP and without a NAT gateway the task cannot
    # reach ECR, and it fails in PROVISIONING with a timeout that names nothing.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.container_port
  }

  # The API opens its pool and touches both databases at startup; 60s of grace
  # keeps a cold start from being registered as a health-check failure.
  health_check_grace_period_seconds = 60

  # A bad image otherwise takes the demo URL down for however long it takes
  # somebody to notice — and nobody is watching this between Aug 19 and Sep 15.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # An ALB target group must already be attached to a load balancer before a
  # service may reference it.
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name_prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = local.subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }

  # 0 / 100 rather than 100 / 200: workers are interchangeable queue consumers,
  # so a deploy that briefly runs fewer of them costs latency, never
  # correctness — the claim protocol is what makes that true.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
