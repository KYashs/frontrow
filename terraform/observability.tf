# Log storage for Loki (deploy/platform/loki.yaml). Loki deletes chunks after
# 7 days; the lifecycle rule is a backstop.

resource "aws_s3_bucket" "logs" {
  bucket_prefix = "${var.project}-logs-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration { days = 14 }
  }
}
