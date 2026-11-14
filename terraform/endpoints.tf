# Gateway endpoints (free): S3 and DynamoDB traffic from the nodes stays on the
# AWS network and skips the NAT gateway's per-GB charge. That includes the API's
# seat traffic and ECR image layers, which are served from S3.
#
# The interface endpoints the EC2 design needed (SQS, Secrets Manager, SSM) are
# gone: with a NAT gateway they cost ~$2.60/day for traffic that is a few KB.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.project}-s3" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.project}-dynamodb" }
}
