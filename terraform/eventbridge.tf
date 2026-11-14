# EventBridge: the checkout worker publishes OrderConfirmed / OrderFailed and
# knows nothing about who listens. Today: email + analytics. Tomorrow:
# loyalty points, fraud checks, CRM, without touching checkout.

resource "aws_cloudwatch_event_bus" "orders" {
  name = "${var.project}-orders"
}

# OrderConfirmed -> notifier Lambda -> SES
resource "aws_cloudwatch_event_rule" "confirmed" {
  name           = "${var.project}-order-confirmed"
  event_bus_name = aws_cloudwatch_event_bus.orders.name
  event_pattern = jsonencode({
    source        = ["frontrow.checkout"]
    "detail-type" = ["OrderConfirmed"]
  })
}

resource "aws_cloudwatch_event_target" "notifier" {
  rule           = aws_cloudwatch_event_rule.confirmed.name
  event_bus_name = aws_cloudwatch_event_bus.orders.name
  arn            = aws_lambda_function.fn["notifier"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }

  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

resource "aws_lambda_permission" "notifier" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["notifier"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.confirmed.arn
}

# Every order outcome -> Firehose -> S3 data lake (analytics.tf)
resource "aws_cloudwatch_event_rule" "all_orders" {
  name           = "${var.project}-orders-to-lake"
  event_bus_name = aws_cloudwatch_event_bus.orders.name
  event_pattern = jsonencode({
    source        = ["frontrow.checkout"]
    "detail-type" = ["OrderConfirmed", "OrderFailed"]
  })
}

resource "aws_cloudwatch_event_target" "firehose" {
  rule           = aws_cloudwatch_event_rule.all_orders.name
  event_bus_name = aws_cloudwatch_event_bus.orders.name
  arn            = aws_kinesis_firehose_delivery_stream.orders.arn
  role_arn       = aws_iam_role.events_to_firehose.arn
  input_path     = "$.detail" # just the order facts, one JSON object per record

  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}

# Hold sweeper on a one-minute schedule
resource "aws_cloudwatch_event_rule" "sweeper" {
  name                = "${var.project}-hold-sweeper"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "sweeper" {
  rule = aws_cloudwatch_event_rule.sweeper.name
  arn  = aws_lambda_function.fn["hold_sweeper"].arn
}

resource "aws_lambda_permission" "sweeper" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["hold_sweeper"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweeper.arn
}
