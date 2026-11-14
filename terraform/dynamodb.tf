# Six tables, each shaped by its access pattern (see core/keys.py).
# All on-demand: an on-sale goes from zero to thousands of writes/s in seconds,
# with no capacity planning or warm-up.

resource "aws_dynamodb_table" "seats" {
  name             = "${var.project}-seats"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "pk" # "<event>#<section>": spreads an on-sale across ~150 partitions
  range_key        = "sk" # "R05#S12"
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES" # stream processor needs old+new status

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "inventory" {
  name             = "${var.project}-inventory"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "pk" # event_id
  range_key        = "sk" # SEC#<section> | GA#<section>#<shard> | META
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "holds" {
  name         = "${var.project}-holds"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "hold_id"

  attribute {
    name = "hold_id"
    type = "S"
  }
  attribute {
    name = "status"
    type = "S"
  }
  attribute {
    name = "expires_at"
    type = "N"
  }

  # The sweeper asks: "which active holds expired before now?"
  global_secondary_index {
    name            = "by_status"
    hash_key        = "status"
    range_key       = "expires_at"
    projection_type = "KEYS_ONLY"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "orders" {
  name         = "${var.project}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  point_in_time_recovery { enabled = true } # orders are the money table

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "waiting_room" {
  name         = "${var.project}-waiting-room"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk" # event_id
  range_key    = "sk" # CONFIG | Q#<shard>

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "connections" {
  name         = "${var.project}-ws-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connection_id"

  attribute {
    name = "connection_id"
    type = "S"
  }
  attribute {
    name = "event_id"
    type = "S"
  }

  global_secondary_index {
    name            = "by_event"
    hash_key        = "event_id"
    projection_type = "KEYS_ONLY"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

locals {
  table_env = {
    SEATS_TABLE       = aws_dynamodb_table.seats.name
    INVENTORY_TABLE   = aws_dynamodb_table.inventory.name
    HOLDS_TABLE       = aws_dynamodb_table.holds.name
    ORDERS_TABLE      = aws_dynamodb_table.orders.name
    WAITING_TABLE     = aws_dynamodb_table.waiting_room.name
    CONNECTIONS_TABLE = aws_dynamodb_table.connections.name
  }
}
