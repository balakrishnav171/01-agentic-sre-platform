variable "alert_email"    { type = string; default = "sre-team@example.com" }
variable "alert_webhook"  { type = string; default = "" }

resource "azurerm_log_analytics_workspace" "main" {
  name                = "sre-platform-law-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

resource "azurerm_application_insights" "api" {
  name                = "sre-platform-appinsights-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  retention_in_days   = 30
  tags                = var.tags
}

resource "azurerm_monitor_action_group" "sre" {
  name                = "sre-alerts-${var.environment}"
  resource_group_name = var.resource_group_name
  short_name          = "sre-alerts"

  email_receiver {
    name                    = "sre-team"
    email_address           = var.alert_email
    use_common_alert_schema = true
  }

  dynamic "webhook_receiver" {
    for_each = var.alert_webhook != "" ? [1] : []
    content {
      name                    = "sre-webhook"
      service_uri             = var.alert_webhook
      use_common_alert_schema = true
    }
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "aks_cpu" {
  name                = "aks-cpu-high-${var.environment}"
  resource_group_name = var.resource_group_name
  scopes              = [azurerm_kubernetes_cluster.main.id]
  description         = "AKS average CPU > 80%"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.ContainerService/managedClusters"
    metric_name      = "node_cpu_usage_percentage"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.sre.id
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "aks_memory" {
  name                = "aks-memory-high-${var.environment}"
  resource_group_name = var.resource_group_name
  scopes              = [azurerm_kubernetes_cluster.main.id]
  description         = "AKS average memory > 80%"
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.ContainerService/managedClusters"
    metric_name      = "node_memory_working_set_percentage"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = 80
  }

  action {
    action_group_id = azurerm_monitor_action_group.sre.id
  }

  tags = var.tags
}

output "log_analytics_workspace_id"   { value = azurerm_log_analytics_workspace.main.id }
output "log_analytics_workspace_name" { value = azurerm_log_analytics_workspace.main.name }
output "app_insights_key"             { value = azurerm_application_insights.api.instrumentation_key; sensitive = true }
output "app_insights_connection_string" { value = azurerm_application_insights.api.connection_string; sensitive = true }
