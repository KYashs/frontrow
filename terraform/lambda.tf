# Six Lambda functions, all outside the VPC (they only call regional AWS
# APIs), all with X-Ray tracing. Zips are built by scripts/build.sh and
# include the shared core/ package (and the catalogue, for the seeder).

locals {
  functions = {
    checkout_worker = {
      description = "SQS FIFO checkout: payment, confirm seats, publish OrderConfirmed"
      timeout     = 30
      memory      = 512
      env         = { EVENT_BUS = aws_cloudwatch_event_bus.orders.name }
    }
    stream_processor = {
      description = "DynamoDB Streams: maintain section counters, push live updates over WebSocket"
      timeout     = 60
      memory      = 512
      env         = { WS_ENDPOINT = "https://${aws_apigatewayv2_api.live.id}.execute-api.${var.region}.amazonaws.com/${aws_apigatewayv2_stage.live.name}" }
    }
    ws_connections = {
      description = "WebSocket $connect/$disconnect: track who is watching which event"
      timeout     = 10
      memory      = 256
      env         = {}
    }
    notifier = {
      description = "EventBridge OrderConfirmed: send the ticket email via SES"
      timeout     = 30
      memory      = 256
      env         = { SENDER_EMAIL = var.sender_email }
    }
    hold_sweeper = {
      description = "Every minute: release expired holds back to inventory"
      timeout     = 120
      memory      = 256
      env         = {}
    }
    inventory_seeder = {
      description = "One-off: create seat, counter and waiting-room items for every event"
      timeout     = 900
      memory      = 1769 # one full vCPU for the parallel batch writes
      env         = {}
    }
  }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.project}-${replace(each.key, "_", "-")}"
  retention_in_days = 14
}

resource "aws_lambda_function" "fn" {
  for_each = local.functions

  function_name    = "${var.project}-${replace(each.key, "_", "-")}"
  description      = each.value.description
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"] # Graviton: cheaper per ms, pure-Python code
  handler          = "handler.handler"
  filename         = "${path.module}/../build/functions/${each.key}.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/functions/${each.key}.zip")
  timeout          = each.value.timeout
  memory_size      = each.value.memory

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = merge(local.table_env, { HOLD_SECONDS = tostring(var.hold_seconds) }, each.value.env)
  }

  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy.fn]
}

# ---- checkout: SQS FIFO -> Lambda ----
resource "aws_lambda_event_source_mapping" "checkout" {
  event_source_arn        = aws_sqs_queue.checkout.arn
  function_name           = aws_lambda_function.fn["checkout_worker"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 50
  }
}

# ---- realtime: DynamoDB Streams -> Lambda ----
resource "aws_lambda_event_source_mapping" "streams" {
  for_each = {
    seats     = aws_dynamodb_table.seats.stream_arn
    inventory = aws_dynamodb_table.inventory.stream_arn
  }

  event_source_arn                   = each.value
  function_name                      = aws_lambda_function.fn["stream_processor"].arn
  starting_position                  = "LATEST"
  batch_size                         = 500
  maximum_batching_window_in_seconds = 1 # coalesce a burst into one counter write per section
  parallelization_factor             = 4 # still ordered per item key
  bisect_batch_on_function_error     = true
  maximum_retry_attempts             = 5

  # Seeding writes 100k+ INSERTs; only status changes matter here.
  filter_criteria {
    filter {
      pattern = jsonencode({ eventName = ["MODIFY"] })
    }
  }
}

# ---- one-off inventory seeding, re-run when the catalogue changes ----
resource "aws_lambda_invocation" "seed_inventory" {
  function_name = aws_lambda_function.fn["inventory_seeder"].function_name
  input         = jsonencode({})

  triggers = {
    catalog = filemd5("${path.module}/../catalog/events.json")
    venues  = sha1(join("", [for f in fileset("${path.module}/../venues", "*.json") : filemd5("${path.module}/../venues/${f}")]))
  }

  depends_on = [aws_lambda_event_source_mapping.streams]
}

# SES: verification link is emailed on apply. In the sandbox, recipients must be verified too.
resource "aws_ses_email_identity" "sender" {
  email = var.sender_email
}
