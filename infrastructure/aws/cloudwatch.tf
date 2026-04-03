variable "cluster_name"     { type = string; default = "sre-platform-eks" }
variable "sns_email"        { type = string; default = "sre-alerts@example.com" }
variable "db_instance_id"   { type = string; default = "" }

resource "aws_cloudwatch_log_group" "eks_cluster" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 30
  tags              = merge(var.tags, { Name = "eks-cluster-logs" })
}

resource "aws_sns_topic" "sre_alerts" {
  name         = "sre-platform-alerts"
  display_name = "SRE Platform Alerts"
  tags         = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.sre_alerts.arn
  protocol  = "email"
  endpoint  = var.sns_email
}

# EKS node CPU alarm
resource "aws_cloudwatch_metric_alarm" "node_cpu_high" {
  alarm_name          = "${var.cluster_name}-node-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "node_cpu_utilization"
  namespace           = "ContainerInsights"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "EKS node CPU > 80% for 10 minutes"
  alarm_actions       = [aws_sns_topic.sre_alerts.arn]
  ok_actions          = [aws_sns_topic.sre_alerts.arn]
  dimensions          = { ClusterName = var.cluster_name }
  tags                = var.tags
}

# EKS node memory alarm
resource "aws_cloudwatch_metric_alarm" "node_memory_high" {
  alarm_name          = "${var.cluster_name}-node-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "node_memory_utilization"
  namespace           = "ContainerInsights"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "EKS node memory > 80%"
  alarm_actions       = [aws_sns_topic.sre_alerts.arn]
  dimensions          = { ClusterName = var.cluster_name }
  tags                = var.tags
}

# Pod restart alarm (CrashLoopBackOff proxy)
resource "aws_cloudwatch_metric_alarm" "pod_restarts" {
  alarm_name          = "${var.cluster_name}-pod-restarts"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "pod_number_of_container_restarts"
  namespace           = "ContainerInsights"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Pods restarting > 5 times in 5 minutes — possible CrashLoopBackOff"
  alarm_actions       = [aws_sns_topic.sre_alerts.arn]
  dimensions          = { ClusterName = var.cluster_name }
  tags                = var.tags
}

# RDS CPU alarm
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  count               = var.db_instance_id != "" ? 1 : 0
  alarm_name          = "sre-platform-rds-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 70
  alarm_description   = "RDS CPU > 70%"
  alarm_actions       = [aws_sns_topic.sre_alerts.arn]
  dimensions          = { DBInstanceIdentifier = var.db_instance_id }
  tags                = var.tags
}

# CloudWatch Dashboard
resource "aws_cloudwatch_dashboard" "sre_platform" {
  dashboard_name = "SRE-Platform-Overview"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "EKS Node CPU Utilization"
          metrics = [["ContainerInsights", "node_cpu_utilization", "ClusterName", var.cluster_name]]
          period = 300, stat = "Average", view = "timeSeries"
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "EKS Node Memory Utilization"
          metrics = [["ContainerInsights", "node_memory_utilization", "ClusterName", var.cluster_name]]
          period = 300, stat = "Average", view = "timeSeries"
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Pod Restarts (CrashLoopBackOff Indicator)"
          metrics = [["ContainerInsights", "pod_number_of_container_restarts", "ClusterName", var.cluster_name]]
          period = 300, stat = "Sum", view = "timeSeries"
        }
      },
      {
        type = "alarm", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Active Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.node_cpu_high.arn,
            aws_cloudwatch_metric_alarm.node_memory_high.arn,
            aws_cloudwatch_metric_alarm.pod_restarts.arn,
          ]
        }
      }
    ]
  })
}

output "sns_topic_arn"      { value = aws_sns_topic.sre_alerts.arn }
output "dashboard_url"      { value = "https://console.aws.amazon.com/cloudwatch/home#dashboards:name=${aws_cloudwatch_dashboard.sre_platform.dashboard_name}" }
output "log_group_name"     { value = aws_cloudwatch_log_group.eks_cluster.name }
