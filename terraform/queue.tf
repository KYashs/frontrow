# SQS FIFO checkout queue. Message group = hold_id: once seats are held there
# is no contention left, so FIFO's job is exactly-once-per-hold processing
# and per-hold ordering, with full parallelism across holds.

resource "aws_sqs_queue" "checkout_dlq" {
  name                      = "${var.project}-checkout-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "checkout" {
  name                        = "${var.project}-checkout.fifo"
  fifo_queue                  = true
  content_based_deduplication = false # API sets MessageDeduplicationId = order_id
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId" # high-throughput FIFO
  visibility_timeout_seconds  = 180                 # 6x the worker timeout
  message_retention_seconds   = 86400
  receive_wait_time_seconds   = 20
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.checkout_dlq.arn
    maxReceiveCount     = 3
  })
}

# Failed EventBridge deliveries (e.g. notifier errors after retries) land here.
resource "aws_sqs_queue" "events_dlq" {
  name                      = "${var.project}-events-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

data "aws_iam_policy_document" "events_dlq" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.events_dlq.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sqs_queue_policy" "events_dlq" {
  queue_url = aws_sqs_queue.events_dlq.id
  policy    = data.aws_iam_policy_document.events_dlq.json
}
