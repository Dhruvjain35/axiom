# AXIOM :: image registry.
#
# One repository for one image. The API and the worker are the same build with
# different CMDs (see the Dockerfile), so there is nothing to keep in sync.

resource "aws_ecr_repository" "axiom" {
  count                = var.create_ecr ? 1 : 0
  name                 = var.name_prefix
  image_tag_mutability = "IMMUTABLE" # a tag that can be moved is not a pin

  image_scanning_configuration {
    scan_on_push = true # basic scanning is free
  }
}

# Untagged layers accumulate on every rebuild and are billed at $0.10/GB-month.
# Ten tagged images is enough history to roll back through a bad week.
resource "aws_ecr_lifecycle_policy" "axiom" {
  count      = var.create_ecr ? 1 : 0
  repository = aws_ecr_repository.axiom[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "expire untagged images after a day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "keep the last 10 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      },
    ]
  })
}
