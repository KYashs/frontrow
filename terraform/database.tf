# RDS PostgreSQL: the relational catalogue (venues, events, price tiers,
# per-event section pricing). Private subnets, reachable from the EKS cluster only.

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.private_db[*].id
}

resource "aws_db_parameter_group" "main" {
  name_prefix = "${var.project}-pg16-"
  family      = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "250" # log slow queries (ms)
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name                     = "frontrow"
  username                    = "frontrow_admin"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period    = 1
  auto_minor_version_upgrade = true
  apply_immediately          = true

  skip_final_snapshot = true # demo: clean destroy
  deletion_protection = false
}
