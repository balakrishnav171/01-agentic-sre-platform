data "azurerm_client_config" "current" {}

variable "keyvault_name" { type = string; default = "sre-platform-kv" }
variable "aks_kubelet_identity_object_id" { type = string; default = "" }

resource "azurerm_key_vault" "main" {
  name                        = "${var.keyvault_name}-${var.environment}"
  location                    = var.location
  resource_group_name         = var.resource_group_name
  tenant_id                   = data.azurerm_client_config.current.tenant_id
  sku_name                    = "standard"
  soft_delete_retention_days  = 7
  purge_protection_enabled    = true
  enable_rbac_authorization   = false

  network_acls {
    default_action             = "Deny"
    bypass                     = "AzureServices"
    virtual_network_subnet_ids = []
    ip_rules                   = []
  }

  tags = merge(var.tags, { Name = "${var.keyvault_name}-${var.environment}" })
}

# Access policy for current Terraform user
resource "azurerm_key_vault_access_policy" "terraform" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  secret_permissions      = ["Get", "List", "Set", "Delete", "Recover", "Backup", "Restore", "Purge"]
  key_permissions         = ["Get", "List", "Create", "Delete", "Update", "Recover"]
  certificate_permissions = ["Get", "List", "Create", "Delete", "Update"]
}

# Access policy for AKS kubelet identity
resource "azurerm_key_vault_access_policy" "aks" {
  count        = var.aks_kubelet_identity_object_id != "" ? 1 : 0
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = var.aks_kubelet_identity_object_id
  secret_permissions = ["Get", "List"]
}

resource "azurerm_key_vault_secret" "datadog_api_key" {
  name         = "datadog-api-key"
  value        = "PLACEHOLDER-SET-MANUALLY"
  key_vault_id = azurerm_key_vault.main.id
  tags         = var.tags
  lifecycle { ignore_changes = [value] }
}

resource "azurerm_key_vault_secret" "openai_api_key" {
  name         = "openai-api-key"
  value        = "PLACEHOLDER-SET-MANUALLY"
  key_vault_id = azurerm_key_vault.main.id
  tags         = var.tags
  lifecycle { ignore_changes = [value] }
}

resource "azurerm_key_vault_secret" "servicenow_password" {
  name         = "servicenow-password"
  value        = "PLACEHOLDER-SET-MANUALLY"
  key_vault_id = azurerm_key_vault.main.id
  tags         = var.tags
  lifecycle { ignore_changes = [value] }
}

output "keyvault_id"   { value = azurerm_key_vault.main.id }
output "keyvault_uri"  { value = azurerm_key_vault.main.vault_uri }
output "keyvault_name" { value = azurerm_key_vault.main.name }
