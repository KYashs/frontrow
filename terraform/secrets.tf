# HMAC key for waiting-room tickets and access passes. The RDS master
# password is generated and stored by RDS itself (manage_master_user_password).

resource "random_password" "token_key" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "token_key" {
  name_prefix             = "${var.project}/token-signing-"
  description             = "Signs waiting-room tickets and access passes"
  recovery_window_in_days = 0 # demo: allow clean destroy/recreate
}

resource "aws_secretsmanager_secret_version" "token_key" {
  secret_id     = aws_secretsmanager_secret.token_key.id
  secret_string = jsonencode({ key = random_password.token_key.result })
}
