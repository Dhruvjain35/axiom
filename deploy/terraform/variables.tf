# AXIOM :: inputs.
#
# Defaults are chosen so that `terraform apply -var image_uri=...` produces a
# working, publicly reachable demo for roughly $32/month (deploy/COST.md has the
# arithmetic). Every default that costs money is the cheapest one that still
# demos well, and says so.

variable "region" {
  description = "AWS region. Must be one where Bedrock serves both model ids."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. Keep it short; ALB names cap at 32 chars."
  type        = string
  default     = "axiom"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,16}$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric/hyphen, 2-17 chars."
  }
}

variable "image_uri" {
  description = <<-EOT
    Fully qualified image URI including tag, e.g.
    704229156617.dkr.ecr.us-east-1.amazonaws.com/axiom:8f3c1de.

    Pin a tag, never `latest`. ECS caches by digest at task-definition
    registration time, so `latest` gives you a fleet where the API and the
    workers are running different builds of tasks.py after a partial rollout —
    which is exactly the class of bug this project exists to argue against.
  EOT
  type        = string
}

# ------------------------------------------------------------------- network --

variable "vpc_id" {
  description = "VPC to deploy into. Empty string selects the account's default VPC."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = <<-EOT
    Public subnets, two or more AZs (an ALB requires at least two).

    PUBLIC, deliberately. Fargate tasks run with assign_public_ip = true so they
    can reach ECR, Bedrock, SSM and CockroachDB Cloud directly. The private-subnet
    alternative needs a NAT gateway at ~$33/month plus data processing — more than
    the entire rest of this deployment — or four interface VPC endpoints at
    ~$7/month each. For a demo whose only ingress is an ALB with a locked-down
    security group, neither is worth it. Empty list selects the default VPC's
    default subnets.
  EOT
  type        = list(string)
  default     = []
}

variable "allowed_cidr" {
  description = "CIDR permitted to reach the ALB. 0.0.0.0/0 because judges are anonymous."
  type        = string
  default     = "0.0.0.0/0"
}

variable "acm_certificate_arn" {
  description = <<-EOT
    ACM certificate for HTTPS. Empty string serves the demo over plain HTTP on
    port 80, which is what an ALB DNS name (*.elb.amazonaws.com) can do without
    a domain you control. ACM certificates are free; the domain is not.
  EOT
  type        = string
  default     = ""
}

# --------------------------------------------------------------------- sizing --

variable "cpu_architecture" {
  description = <<-EOT
    ARM64 or X86_64. ARM64 is the default because Fargate Graviton is ~20%
    cheaper per vCPU-hour and Python has manylinux aarch64 wheels for every
    dependency in requirements.txt. Your build host must produce a matching
    image: `docker buildx build --platform linux/arm64`.
  EOT
  type        = string
  default     = "ARM64"

  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task. 256 = 0.25 vCPU, the smallest Fargate sells."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "MiB for the API task. 512 is the minimum permitted with 256 CPU units."
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "Fargate CPU units per worker agent."
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "MiB per worker agent."
  type        = number
  default     = 512
}

variable "worker_count" {
  description = <<-EOT
    Number of worker agents. TWO, not one — the demo's whole argument is what
    happens when a worker dies mid-flight, and with a single worker there is
    nobody to observe the orphaned lease expire and recover the task. Two is the
    smallest number that makes the recovery path visible on camera. Each
    additional worker is ~$7.20/month.
  EOT
  type        = number
  default     = 2
}

# -------------------------------------------------------------------- secrets --

variable "database_url_parameter_name" {
  description = <<-EOT
    SSM Parameter Store name (SecureString) holding the CockroachDB Cloud DSN.

    Created OUT OF BAND by scripts/deploy.sh, and referenced here only by ARN.
    That is on purpose: a `data "aws_ssm_parameter"` lookup writes the decrypted
    value into terraform.tfstate, and a plaintext database password in a state
    file is worse than no secret manager at all. Standard-tier parameters are
    free, which is why this is not Secrets Manager ($0.40/secret/month).
  EOT
  type        = string
  default     = "/axiom/prod/DATABASE_URL"
}

variable "provider_database_url_parameter_name" {
  description = <<-EOT
    SSM parameter holding PROVIDER_DATABASE_URL. Empty string omits it, and
    axiom/provider.py then derives the provider DSN from DATABASE_URL by swapping
    the database component — correct whenever the stand-in provider lives on the
    same cluster in its own database.
  EOT
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = <<-EOT
    Customer-managed KMS key that encrypts the SSM parameters, if any. Empty
    string means the AWS-managed `alias/aws/ssm` key, for which ECS needs no
    explicit kms:Decrypt grant. A customer-managed key costs $1/month and buys
    nothing for a demo.
  EOT
  type        = string
  default     = ""
}

# --------------------------------------------------------------------- bedrock --

variable "bedrock_embed_model_id" {
  description = "Must match AXIOM_EMBED_MODEL. The IAM policy is scoped to exactly this id."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "bedrock_llm_model_id" {
  description = "Must match AXIOM_LLM_MODEL. The IAM policy is scoped to exactly this id."
  type        = string
  default     = "anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "extra_bedrock_model_arns" {
  description = <<-EOT
    Additional Bedrock ARNs the task role may invoke.

    Needed when a model is only reachable through a cross-region inference
    profile: the call then authorizes against BOTH the inference-profile ARN
    (arn:aws:bedrock:REGION:ACCOUNT:inference-profile/us.anthropic....) and the
    foundation-model ARN in every region the profile can route to. Leave empty
    if you invoke the bare model id and it works.
  EOT
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------- misc --

variable "axiom_offline" {
  description = <<-EOT
    true runs the deterministic embedding + triage stand-ins and never calls
    Bedrock. false is correct for the deployed demo — the point of deploying is
    that the AWS integration is real.
  EOT
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention. 7 days covers the entire Aug 19 - Sep 15 judging
    window on a rolling basis at a storage cost under a nickel. The default of
    "never expire" is how a demo quietly turns into a monthly bill.
  EOT
  type        = number
  default     = 7
}

variable "create_ecr" {
  description = "Create the ECR repository here. Set false if scripts/deploy.sh already made it."
  type        = bool
  default     = true
}

variable "enable_execute_command" {
  description = <<-EOT
    ECS Exec. This is the single most demo-critical flag in the module: it is how
    you get a shell inside a running Fargate worker and SIGKILL its process on
    camera —

        aws ecs execute-command --cluster axiom --task <arn> \
            --container worker --interactive --command "/bin/sh"
        kill -9 1

    which is the reason this project is on Fargate and not Lambda. Requires the
    ssmmessages grants on the task role (see iam.tf).
  EOT
  type        = bool
  default     = true
}
