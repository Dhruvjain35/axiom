# AXIOM :: VPC selection and the two security groups.
#
# There is no aws_vpc resource here and that is the point. A purpose-built VPC
# for a four-week demo buys isolation nobody is threatening and costs either a
# NAT gateway or four interface endpoints. The default VPC already has public
# subnets in every AZ with an internet gateway attached.

data "aws_vpc" "default" {
  count   = var.vpc_id == "" ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = length(var.subnet_ids) == 0 ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }

  # default-for-az subnets in a default VPC are the ones with a route to the
  # internet gateway. Without this filter you can select a private subnet, and
  # the failure mode is a Fargate task that sits in PROVISIONING until it times
  # out pulling from ECR — with no error that names the cause.
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

locals {
  vpc_id = var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default[0].id

  # TWO subnets, not all of them. An ALB provisions one public IPv4 address per
  # enabled subnet and AWS bills $0.005/hour for every in-use public IPv4 — so
  # spreading across the default VPC's six AZs costs $21.90/month in addresses
  # alone, versus $7.30 for the two that an ALB minimally requires. sort() only
  # makes the choice deterministic across plans; if Fargate rejects one of the
  # AZs it picks ("Fargate is not supported in this availability zone" —
  # us-east-1e is the usual offender), set subnet_ids explicitly.
  subnet_ids = length(var.subnet_ids) > 0 ? var.subnet_ids : slice(sort(data.aws_subnets.default[0].ids), 0, 2)

  container_port = 8000
}

# ------------------------------------------------------------------- ALB SG --

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "AXIOM ALB: public HTTP(S) in, container port out"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from the internet (redirected to HTTPS when a cert is set)"
  cidr_ipv4         = var.allowed_cidr
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count             = var.acm_certificate_arn != "" ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  cidr_ipv4         = var.allowed_cidr
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Only to the API tasks, only on the container port"
  referenced_security_group_id = aws_security_group.service.id
  from_port                    = local.container_port
  to_port                      = local.container_port
  ip_protocol                  = "tcp"
}

# --------------------------------------------------------------- service SG --

resource "aws_security_group" "service" {
  name        = "${var.name_prefix}-service"
  description = "AXIOM tasks: ALB in on 8000, all out"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "service_from_alb" {
  security_group_id = aws_security_group.service.id
  # Source is the ALB's security group, not a CIDR. The tasks hold public IPs
  # (they must, to reach ECR without a NAT gateway), so a CIDR-based rule here
  # would leave port 8000 open to the internet and bypass the load balancer.
  description                  = "API traffic from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = local.container_port
  to_port                      = local.container_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "service_all" {
  security_group_id = aws_security_group.service.id
  # Egress stays open: the tasks need ECR, CloudWatch Logs, SSM, Bedrock, the
  # SSM messages channel for ECS Exec, and CockroachDB Cloud on 26257 — an
  # allowlist of AWS service prefixes here would need maintaining every time a
  # service adds an endpoint, and the ingress rule is what actually protects
  # these tasks.
  description = "Outbound to AWS APIs and CockroachDB Cloud"
  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
}
