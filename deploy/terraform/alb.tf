# AXIOM :: the demo URL.
#
# The submission requires a functional demo URL that answers requests from
# Aug 19 to Sep 15. An ALB is $16.43/month of that bill and it is the line item
# most worth interrogating — see deploy/COST.md for the alternative that drops
# it entirely and what you give up.
#
# What the ALB actually buys: a DNS name that survives task replacement. A
# Fargate task's public IP changes every time it restarts, and the demo's
# premise is that things restart.

resource "aws_lb" "main" {
  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = local.subnet_ids

  # Judging runs for four weeks unattended. Deletion protection off is correct
  # anyway (teardown must be one command), but idle timeout matters: the default
  # 60s is longer than any AXIOM endpoint takes, and lowering it would truncate
  # the recall endpoint's EXPLAIN round-trip under a cold vector index.
  idle_timeout               = 60
  drop_invalid_header_fields = true
  enable_deletion_protection = false
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name_prefix}-api"
  port        = local.container_port
  protocol    = "HTTP"
  vpc_id      = local.vpc_id
  target_type = "ip" # awsvpc networking: targets are ENI addresses, not instances

  health_check {
    enabled = true
    path    = "/api/health"
    matcher = "200"

    # /api/health opens a real transaction against CockroachDB and a real
    # connection to the provider database. It returns 200 with {"ok": false}
    # when a dependency is down rather than 503, because a task that cannot
    # reach the cluster should still be able to TELL you that — replacing it
    # would not fix a cluster-side outage, it would just hide it. So the ALB
    # checks liveness here; correctness of the dependencies is reported in the
    # body and surfaced in Mission Control.
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # 30s instead of the 300s default: a rolling deploy of a two-task service
  # otherwise spends five minutes draining a connection nobody is holding.
  deregistration_delay = 30
}

# ------------------------------------------------------------------ listener --

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, port 80 exists only to redirect. Without one, it is the
  # demo — an *.elb.amazonaws.com name cannot have a public certificate, so
  # plain HTTP is the honest default rather than a self-signed certificate that
  # makes a judge click through a browser warning.
  dynamic "default_action" {
    for_each = var.acm_certificate_arn != "" ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.acm_certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
