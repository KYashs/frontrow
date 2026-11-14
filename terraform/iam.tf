# IAM "key cards": every component gets exactly the actions its code makes,
# on exactly the resources it touches. Nothing has "*" on DynamoDB.

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  t = {
    seats       = aws_dynamodb_table.seats.arn
    inventory   = aws_dynamodb_table.inventory.arn
    holds       = aws_dynamodb_table.holds.arn
    orders      = aws_dynamodb_table.orders.arn
    waiting     = aws_dynamodb_table.waiting_room.arn
    connections = aws_dynamodb_table.connections.arn
  }
}

# ------------------------------------------------------------------ Lambdas
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${var.project}-${replace(each.key, "_", "-")}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "fn_xray" {
  for_each   = local.functions
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

data "aws_iam_policy_document" "checkout_worker" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.checkout.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"]
    resources = [local.t.seats, local.t.inventory, local.t.holds, local.t.orders]
  }
  statement {
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.orders.arn]
  }
}

data "aws_iam_policy_document" "stream_processor" {
  statement {
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
    resources = [aws_dynamodb_table.seats.stream_arn, aws_dynamodb_table.inventory.stream_arn]
  }
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [local.t.inventory]
  }
  statement {
    actions   = ["dynamodb:Query", "dynamodb:DeleteItem"]
    resources = [local.t.connections, "${local.t.connections}/index/by_event"]
  }
  statement {
    actions   = ["execute-api:ManageConnections"]
    resources = ["${aws_apigatewayv2_api.live.execution_arn}/${aws_apigatewayv2_stage.live.name}/POST/@connections/*"]
  }
}

data "aws_iam_policy_document" "ws_connections" {
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [local.t.connections]
  }
}

data "aws_iam_policy_document" "notifier" {
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [local.t.orders]
  }
  statement {
    actions   = ["ses:SendEmail"]
    resources = ["arn:aws:ses:${var.region}:${local.account}:identity/*"]
  }
}

data "aws_iam_policy_document" "hold_sweeper" {
  statement {
    actions   = ["dynamodb:Query"]
    resources = ["${local.t.holds}/index/by_status"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [local.t.holds, local.t.seats, local.t.inventory]
  }
}

data "aws_iam_policy_document" "inventory_seeder" {
  statement {
    actions   = ["dynamodb:BatchWriteItem", "dynamodb:PutItem", "dynamodb:GetItem"]
    resources = [local.t.seats, local.t.inventory, local.t.waiting]
  }
}

locals {
  fn_policies = {
    checkout_worker  = data.aws_iam_policy_document.checkout_worker.json
    stream_processor = data.aws_iam_policy_document.stream_processor.json
    ws_connections   = data.aws_iam_policy_document.ws_connections.json
    notifier         = data.aws_iam_policy_document.notifier.json
    hold_sweeper     = data.aws_iam_policy_document.hold_sweeper.json
    inventory_seeder = data.aws_iam_policy_document.inventory_seeder.json
  }
}

data "aws_iam_policy_document" "fn_logs" {
  for_each                = local.functions
  source_policy_documents = [local.fn_policies[each.key]]
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fn[each.key].arn}:*"]
  }
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = "least-privilege"
  role     = aws_iam_role.fn[each.key].id
  policy   = data.aws_iam_policy_document.fn_logs[each.key].json
}

# ------------------------------------------------------------------ service roles
data "aws_iam_policy_document" "firehose_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "firehose" {
  name               = "${var.project}-firehose"
  assume_role_policy = data.aws_iam_policy_document.firehose_assume.json
}

data "aws_iam_policy_document" "firehose" {
  statement {
    actions   = ["s3:PutObject", "s3:GetBucketLocation", "s3:ListBucket", "s3:AbortMultipartUpload", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.lake.arn, "${aws_s3_bucket.lake.arn}/*"]
  }
}

resource "aws_iam_role_policy" "firehose" {
  name   = "write-lake"
  role   = aws_iam_role.firehose.id
  policy = data.aws_iam_policy_document.firehose.json
}

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_to_firehose" {
  name               = "${var.project}-events-to-firehose"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
}

data "aws_iam_policy_document" "events_to_firehose" {
  statement {
    actions   = ["firehose:PutRecord", "firehose:PutRecordBatch"]
    resources = [aws_kinesis_firehose_delivery_stream.orders.arn]
  }
}

resource "aws_iam_role_policy" "events_to_firehose" {
  name   = "put-records"
  role   = aws_iam_role.events_to_firehose.id
  policy = data.aws_iam_policy_document.events_to_firehose.json
}
