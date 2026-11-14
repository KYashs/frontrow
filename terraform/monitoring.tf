# CloudWatch: one on-sale dashboard, alarms on everything that would hurt a
# sale, all routed to SNS. Business metrics come from Embedded Metric Format
# log lines (namespace "FrontRow"), so they cost no API calls.

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  fn_names = { for k, f in aws_lambda_function.fn : k => f.function_name }
  alarms = merge(
    {
      alb-5xx             = { d = "API pods returning 5xx", ns = "AWS/ApplicationELB", m = "HTTPCode_Target_5XX_Count", dims = { LoadBalancer = aws_lb.app.arn_suffix }, stat = "Sum", th = 25, p = 1 }
      unhealthy-hosts     = { d = "Instances failing health checks", ns = "AWS/ApplicationELB", m = "UnHealthyHostCount", dims = { LoadBalancer = aws_lb.app.arn_suffix, TargetGroup = aws_lb_target_group.app.arn_suffix }, stat = "Maximum", th = 0, p = 2 }
      checkout-backlog    = { d = "Checkouts waiting > 60s", ns = "AWS/SQS", m = "ApproximateAgeOfOldestMessage", dims = { QueueName = aws_sqs_queue.checkout.name }, stat = "Maximum", th = 60, p = 2 }
      checkout-dlq        = { d = "Checkouts that failed 3 times", ns = "AWS/SQS", m = "ApproximateNumberOfMessagesVisible", dims = { QueueName = aws_sqs_queue.checkout_dlq.name }, stat = "Maximum", th = 0, p = 1 }
      events-dlq          = { d = "EventBridge deliveries that gave up", ns = "AWS/SQS", m = "ApproximateNumberOfMessagesVisible", dims = { QueueName = aws_sqs_queue.events_dlq.name }, stat = "Maximum", th = 0, p = 1 }
      stream-lag          = { d = "Live seat map falling behind (stream iterator age > 30s)", ns = "AWS/Lambda", m = "IteratorAge", dims = { FunctionName = local.fn_names["stream_processor"] }, stat = "Maximum", th = 30000, p = 2 }
      email-failures      = { d = "SES rejected confirmation emails", ns = "FrontRow", m = "EmailFailures", dims = { Service = "notifier" }, stat = "Sum", th = 0, p = 1 }
      seats-throttled     = { d = "DynamoDB throttling seat writes", ns = "AWS/DynamoDB", m = "WriteThrottleEvents", dims = { TableName = aws_dynamodb_table.seats.name }, stat = "Sum", th = 0, p = 1 }
      inventory-throttled = { d = "DynamoDB throttling inventory counters", ns = "AWS/DynamoDB", m = "WriteThrottleEvents", dims = { TableName = aws_dynamodb_table.inventory.name }, stat = "Sum", th = 0, p = 1 }
    },
    { for k, name in local.fn_names : "${replace(k, "_", "-")}-errors" => { d = "Lambda ${k} errors", ns = "AWS/Lambda", m = "Errors", dims = { FunctionName = name }, stat = "Sum", th = 0, p = 1 } }
  )
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarms

  alarm_name          = "${var.project}-${each.key}"
  alarm_description   = each.value.d
  namespace           = each.value.ns
  metric_name         = each.value.m
  dimensions          = each.value.dims
  statistic           = each.value.stat
  period              = 60
  evaluation_periods  = each.value.p
  threshold           = each.value.th
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "onsale" {
  dashboard_name = "${var.project}-onsale"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 8, height = 6
        properties = {
          title = "Funnel (per minute)", region = var.region, stat = "Sum", period = 60, view = "timeSeries"
          metrics = [
            ["FrontRow", "QueueJoins", "Service", "api"],
            [".", "HoldsCreated", ".", "."],
            [".", "HoldConflicts", ".", "."],
            [".", "CheckoutsStarted", ".", "."],
            [".", "OrdersConfirmed", ".", "checkout"],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 0, width = 8, height = 6
        properties = {
          title = "Tickets sold", region = var.region, stat = "Sum", period = 60, view = "timeSeries"
          metrics = [
            ["FrontRow", "TicketsSold", "Service", "checkout"],
            [".", "PaymentsDeclined", ".", "."],
            [".", "HoldsExpired", ".", "sweeper"],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 0, width = 8, height = 6
        properties = {
          title = "Edge: requests and WAF blocks", region = "us-east-1", stat = "Sum", period = 60, view = "timeSeries"
          metrics = [
            ["AWS/CloudFront", "Requests", "DistributionId", aws_cloudfront_distribution.main.id, "Region", "Global"],
            ["AWS/WAFV2", "BlockedRequests", "WebACL", "${var.project}-edge", "Rule", "ALL", { yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 8, height = 6
        properties = {
          title = "ALB traffic and latency", region = var.region, period = 60, view = "timeSeries"
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.app.arn_suffix, { stat = "Sum" }],
            [".", "TargetResponseTime", ".", ".", { stat = "p95", yAxis = "right" }],
            [".", "HTTPCode_Target_5XX_Count", ".", ".", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 6, width = 8, height = 6
        properties = {
          title = "API pods behind the ALB", region = var.region, period = 60, view = "timeSeries"
          metrics = [
            ["AWS/ApplicationELB", "HealthyHostCount", "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix, { stat = "Minimum" }],
            ["AWS/ApplicationELB", "TargetResponseTime", "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix, { stat = "p95", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 6, width = 8, height = 6
        properties = {
          title = "Checkout queue", region = var.region, stat = "Maximum", period = 60, view = "timeSeries"
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.checkout.name],
            [".", "ApproximateAgeOfOldestMessage", ".", ".", { yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 8, height = 6
        properties = {
          title = "DynamoDB writes and throttles", region = var.region, stat = "Sum", period = 60, view = "timeSeries"
          metrics = [
            ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.seats.name],
            [".", ".", ".", aws_dynamodb_table.inventory.name],
            [".", "WriteThrottleEvents", ".", aws_dynamodb_table.seats.name, { yAxis = "right" }],
            [".", "WriteThrottleEvents", ".", aws_dynamodb_table.inventory.name, { yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 12, width = 8, height = 6
        properties = {
          title = "Live updates", region = var.region, period = 60, view = "timeSeries"
          metrics = [
            ["AWS/ApiGateway", "ConnectCount", "ApiId", aws_apigatewayv2_api.live.id, "Stage", "live", { stat = "Sum" }],
            ["FrontRow", "WsMessagesSent", "Service", "realtime", { stat = "Sum" }],
            ["AWS/Lambda", "IteratorAge", "FunctionName", local.fn_names["stream_processor"], { stat = "Maximum", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 12, width = 8, height = 6
        properties = {
          title   = "Lambda errors", region = var.region, stat = "Sum", period = 60, view = "timeSeries"
          metrics = [for k, name in local.fn_names : ["AWS/Lambda", "Errors", "FunctionName", name, { label = k }]]
        }
      },
    ]
  })
}
