# API Gateway WebSocket API: live seat-map updates.
# Browsers connect to wss://.../live?event=<id>; the stream processor pushes
# messages through the @connections management API.

resource "aws_apigatewayv2_api" "live" {
  name                       = "${var.project}-live"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_integration" "ws" {
  api_id                    = aws_apigatewayv2_api.live.id
  integration_type          = "AWS_PROXY"
  integration_uri           = aws_lambda_function.fn["ws_connections"].invoke_arn
  content_handling_strategy = "CONVERT_TO_TEXT"
}

resource "aws_apigatewayv2_route" "ws" {
  for_each  = toset(["$connect", "$disconnect", "$default"])
  api_id    = aws_apigatewayv2_api.live.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.ws.id}"
}

resource "aws_apigatewayv2_stage" "live" {
  api_id      = aws_apigatewayv2_api.live.id
  name        = "live"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 2000
    throttling_rate_limit  = 1000
  }
}

resource "aws_lambda_permission" "ws" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["ws_connections"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.live.execution_arn}/*/*"
}
