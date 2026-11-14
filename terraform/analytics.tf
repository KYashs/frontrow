# Sales analytics without touching the transactional path:
# EventBridge -> Kinesis Data Firehose -> S3 (partitioned by event and day)
# -> Glue Data Catalog (partition projection) -> Athena SQL.

locals {
  event_ids = [for e in jsondecode(file("${path.module}/../catalog/events.json")).events : e.event_id]
}

resource "aws_s3_bucket" "lake" {
  bucket_prefix = "${var.project}-lake-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter { prefix = "athena-results/" }
    expiration { days = 7 }
  }

  rule {
    id     = "tier-old-orders"
    status = "Enabled"
    filter { prefix = "orders/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }
}

resource "aws_kinesis_firehose_delivery_stream" "orders" {
  name        = "${var.project}-orders"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = aws_s3_bucket.lake.arn
    prefix              = "orders/event_id=!{partitionKeyFromQuery:event_id}/dt=!{timestamp:yyyy-MM-dd}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"
    buffering_size      = 64 # MB (minimum with dynamic partitioning)
    buffering_interval  = 60 # seconds
    compression_format  = "UNCOMPRESSED"

    dynamic_partitioning_configuration {
      enabled = true
    }

    processing_configuration {
      enabled = true

      processors {
        type = "MetadataExtraction"
        parameters {
          parameter_name  = "MetadataExtractionQuery"
          parameter_value = "{event_id:.event_id}"
        }
        parameters {
          parameter_name  = "JsonParsingEngine"
          parameter_value = "JQ-1.6"
        }
      }

      processors {
        type = "AppendDelimiterToRecord" # newline-delimited JSON for Athena
      }
    }
  }
}

resource "aws_glue_catalog_database" "analytics" {
  name = replace("${var.project}_analytics", "-", "_")
}

resource "aws_glue_catalog_table" "orders" {
  name          = "orders"
  database_name = aws_glue_catalog_database.analytics.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"             = "json"
    "projection.enabled"         = "true"
    "projection.event_id.type"   = "enum"
    "projection.event_id.values" = join(",", local.event_ids)
    "projection.dt.type"         = "date"
    "projection.dt.format"       = "yyyy-MM-dd"
    "projection.dt.range"        = "2026-01-01,NOW"
    "storage.location.template"  = "s3://${aws_s3_bucket.lake.id}/orders/event_id=$${event_id}/dt=$${dt}/"
  }

  partition_keys {
    name = "event_id"
    type = "string"
  }
  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.lake.id}/orders/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
    }

    dynamic "columns" {
      for_each = {
        order_id         = "string"
        status           = "string"
        reason           = "string"
        artist           = "string"
        title            = "string"
        venue            = "string"
        city             = "string"
        kind             = "string"
        qty              = "int"
        total            = "bigint"
        currency         = "string"
        tiers            = "array<string>"
        sections         = "array<string>"
        payment          = "string"
        at               = "bigint"
        checkout_seconds = "int"
      }
      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

resource "aws_athena_workgroup" "analytics" {
  name          = "${var.project}-analytics"
  force_destroy = true

  configuration {
    enforce_workgroup_configuration = true
    bytes_scanned_cutoff_per_query  = 1073741824 # 1 GB guard rail

    result_configuration {
      output_location = "s3://${aws_s3_bucket.lake.id}/athena-results/"
    }
  }
}

resource "aws_athena_named_query" "revenue_by_show" {
  name      = "revenue-by-show"
  workgroup = aws_athena_workgroup.analytics.id
  database  = aws_glue_catalog_database.analytics.name
  query     = <<-SQL
    SELECT artist, city, currency,
           SUM(qty)   AS tickets,
           SUM(total) AS revenue,
           ROUND(AVG(checkout_seconds), 1) AS avg_checkout_seconds
    FROM orders
    WHERE status = 'CONFIRMED'
    GROUP BY artist, city, currency
    ORDER BY tickets DESC;
  SQL
}

resource "aws_athena_named_query" "sales_per_minute" {
  name      = "sales-per-minute"
  workgroup = aws_athena_workgroup.analytics.id
  database  = aws_glue_catalog_database.analytics.name
  query     = <<-SQL
    SELECT artist,
           date_trunc('minute', from_unixtime(at)) AS minute,
           SUM(qty) AS tickets
    FROM orders
    WHERE status = 'CONFIRMED'
    GROUP BY 1, 2
    ORDER BY 2 DESC
    LIMIT 200;
  SQL
}

resource "aws_athena_named_query" "failures" {
  name      = "checkout-failures"
  workgroup = aws_athena_workgroup.analytics.id
  database  = aws_glue_catalog_database.analytics.name
  query     = <<-SQL
    SELECT artist, reason, COUNT(*) AS orders
    FROM orders
    WHERE status = 'FAILED'
    GROUP BY artist, reason
    ORDER BY orders DESC;
  SQL
}
