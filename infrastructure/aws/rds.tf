terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

variable "identifier"          { type = string; default = "sre-platform-db" }
variable "engine_version"      { type = string; default = "15.4" }
variable "instance_class"      { type = string; default = "db.t3.medium" }
variable "allocated_storage"   { type = number; default = 100 }
variable "db_name"             { type = string; default = "sreplatform" }
variable "db_username"         { type = string; default = "sreadmin" }
variable "subnet_ids"          { type = list(string) }
variable "vpc_id"              { type = string }
variable "eks_security_group_id" { type = string }
variable "environment"         { type = string; default = "dev" }
variable "tags"                { type = map(string); default = {} }

resource "random_password" "db_password" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = "/${var.environment}/sre-platform/db-password"
  description             = "RDS master password for SRE platform"
  recovery_window_in_days = 7
  tags                    = merge(var.tags, { Name = "sre-platform-db-secret" })
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id = aws_secretsmanager_secret.db_password.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db_password.result
    dbname   = var.db_name
    host     = aws_db_instance.main.address
    port     = 5432
  })
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.identifier}-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = merge(var.tags, { Name = "${var.identifier}-subnet-group" })
}

resource "aws_security_group" "rds" {
  name        = "${var.identifier}-rds-sg"
  description = "Security group for RDS PostgreSQL — allow EKS access only"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.eks_security_group_id]
    description     = "Allow PostgreSQL from EKS nodes"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.identifier}-rds-sg" })
}

resource "aws_db_parameter_group" "postgres15" {
  name        = "${var.identifier}-pg15"
  family      = "postgres15"
  description = "Custom parameter group for SRE platform PostgreSQL 15"

  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
  parameter {
    name  = "log_duration"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
  }
  parameter {
    name  = "max_connections"
    value = "200"
  }

  tags = merge(var.tags, { Name = "${var.identifier}-pg15" })
}

resource "aws_db_instance" "main" {
  identifier        = var.identifier
  engine            = "postgres"
  engine_version    = var.engine_version
  instance_class    = var.instance_class
  allocated_storage = var.allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres15.name

  multi_az               = true
  publicly_accessible    = false
  deletion_protection    = true
  skip_final_snapshot    = false
  final_snapshot_identifier = "${var.identifier}-final-snapshot"

  backup_retention_period   = 7
  backup_window             = "03:00-04:00"
  maintenance_window        = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true

  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_enhanced_monitoring.arn

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = merge(var.tags, { Name = var.identifier })
}

resource "aws_iam_role" "rds_enhanced_monitoring" {
  name = "${var.identifier}-enhanced-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
    }]
  })
  managed_policy_arns = ["arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"]
  tags                = var.tags
}

output "db_endpoint"    { value = aws_db_instance.main.address }
output "db_port"        { value = aws_db_instance.main.port }
output "db_name"        { value = aws_db_instance.main.db_name }
output "secret_arn"     { value = aws_secretsmanager_secret.db_password.arn }
output "db_instance_id" { value = aws_db_instance.main.id }
