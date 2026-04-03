variable "cluster_name"        { type = string; default = "sre-platform-aks" }
variable "kubernetes_version"   { type = string; default = "1.29" }
variable "aks_subnet_id"        { type = string }
variable "log_analytics_workspace_id" { type = string; default = "" }

resource "azurerm_kubernetes_cluster" "main" {
  name                = "${var.cluster_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = "${var.cluster_name}-${var.environment}"
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Standard"

  default_node_pool {
    name                 = "system"
    node_count           = 2
    vm_size              = "Standard_D2_v3"
    vnet_subnet_id       = var.aks_subnet_id
    zones                = ["1", "2", "3"]
    enable_auto_scaling  = true
    min_count            = 2
    max_count            = 5
    os_disk_size_gb      = 50
    os_disk_type         = "Managed"
    type                 = "VirtualMachineScaleSets"
    only_critical_addons_enabled = true
    node_labels = {
      "role" = "system"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin     = "azure"
    network_policy     = "azure"
    load_balancer_sku  = "standard"
    outbound_type      = "userDefinedRouting"
    service_cidr       = "172.16.0.0/16"
    dns_service_ip     = "172.16.0.10"
  }

  azure_active_directory_role_based_access_control {
    managed                = true
    azure_rbac_enabled     = true
  }

  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id != "" ? var.log_analytics_workspace_id : null
  }

  auto_scaler_profile {
    balance_similar_node_groups      = true
    expander                         = "random"
    max_graceful_termination_sec     = 600
    max_unready_nodes                = 3
    scale_down_delay_after_add       = "10m"
    scale_down_unneeded              = "10m"
    scale_down_utilization_threshold = "0.5"
  }

  maintenance_window {
    allowed {
      day   = "Sunday"
      hours = [2, 3, 4]
    }
  }

  tags = merge(var.tags, { Name = "${var.cluster_name}-${var.environment}" })
}

resource "azurerm_kubernetes_cluster_node_pool" "app" {
  name                  = "app"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = "Standard_D4_v3"
  vnet_subnet_id        = var.aks_subnet_id
  zones                 = ["1", "2", "3"]
  enable_auto_scaling   = true
  min_count             = 2
  max_count             = 10
  os_disk_size_gb       = 100
  node_labels           = { "role" = "app" }
  node_taints           = []
  tags                  = var.tags
}

output "aks_cluster_id"              { value = azurerm_kubernetes_cluster.main.id }
output "aks_cluster_name"            { value = azurerm_kubernetes_cluster.main.name }
output "aks_host"                    { value = azurerm_kubernetes_cluster.main.kube_config[0].host; sensitive = true }
output "aks_client_certificate"      { value = azurerm_kubernetes_cluster.main.kube_config[0].client_certificate; sensitive = true }
output "aks_kube_config_raw"         { value = azurerm_kubernetes_cluster.main.kube_config_raw; sensitive = true }
output "aks_kubelet_identity"        { value = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id }
