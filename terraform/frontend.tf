# Static site + venue geometry in a private S3 bucket, readable only by
# CloudFront (Origin Access Control). The geometry is the heaviest payload
# on the page and never changes during a sale, so it lives at the edge.

resource "aws_s3_bucket" "frontend" {
  bucket_prefix = "${var.project}-web-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    sid       = "CloudFrontReadOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json
}

locals {
  web_dir    = "${path.module}/../frontend"
  venue_dir  = "${path.module}/../venues"
  mime_types = { html = "text/html; charset=utf-8", css = "text/css; charset=utf-8", js = "application/javascript; charset=utf-8", json = "application/json", svg = "image/svg+xml", png = "image/png", ico = "image/x-icon" }
}

resource "aws_s3_object" "web" {
  for_each      = fileset(local.web_dir, "**")
  bucket        = aws_s3_bucket.frontend.id
  key           = each.value
  source        = "${local.web_dir}/${each.value}"
  etag          = filemd5("${local.web_dir}/${each.value}")
  content_type  = lookup(local.mime_types, reverse(split(".", each.value))[0], "application/octet-stream")
  cache_control = endswith(each.value, ".html") ? "no-cache" : "public, max-age=300"
}

resource "aws_s3_object" "venues" {
  for_each      = fileset(local.venue_dir, "*.json")
  bucket        = aws_s3_bucket.frontend.id
  key           = "venues/${each.value}"
  source        = "${local.venue_dir}/${each.value}"
  etag          = filemd5("${local.venue_dir}/${each.value}")
  content_type  = "application/json"
  cache_control = "public, max-age=3600"
}

# Runtime config the browser needs: where the WebSocket lives.
resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.frontend.id
  key           = "config.json"
  content       = jsonencode({ ws_url = aws_apigatewayv2_stage.live.invoke_url, region = var.region })
  content_type  = "application/json"
  cache_control = "no-cache"
}
