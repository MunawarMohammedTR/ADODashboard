# setup_azure.ps1
# Run this ONCE to provision Azure infrastructure for the ADO Dashboard.
# Prerequisites: az CLI installed, `az login` completed with your TR account.

$ErrorActionPreference = "Stop"

# ── Configuration ────────────────────────────────────────────────────────────
$ResourceGroup  = "rg-ado-dashboard"
$Location       = "eastus"
$StorageSuffix  = -join ((97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
$StorageAccount = "stadodashboard$StorageSuffix"   # must be globally unique, 3-24 lowercase chars
$AppName        = "ado-dashboard-auth"

# ── 1. Ensure logged in ──────────────────────────────────────────────────────
Write-Host "`nChecking Azure login..." -ForegroundColor Cyan
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Not logged in. Running az login..." -ForegroundColor Yellow
    az login
    $account = az account show | ConvertFrom-Json
}
Write-Host "  Logged in as: $($account.user.name) | Subscription: $($account.name)" -ForegroundColor Green

# ── 2. Resource Group ────────────────────────────────────────────────────────
Write-Host "`nCreating resource group '$ResourceGroup' in $Location..." -ForegroundColor Cyan
az group create --name $ResourceGroup --location $Location --output none
Write-Host "  Done." -ForegroundColor Green

# ── 3. Storage Account ───────────────────────────────────────────────────────
Write-Host "`nCreating storage account '$StorageAccount'..." -ForegroundColor Cyan
az storage account create `
    --name $StorageAccount `
    --resource-group $ResourceGroup `
    --location $Location `
    --sku Standard_LRS `
    --kind StorageV2 `
    --allow-blob-public-access false `
    --output none
Write-Host "  Done." -ForegroundColor Green

# ── 4. Enable Static Website ─────────────────────────────────────────────────
Write-Host "`nEnabling static website hosting..." -ForegroundColor Cyan
az storage blob service-properties update `
    --account-name $StorageAccount `
    --static-website `
    --index-document index.html `
    --404-document index.html `
    --output none
Write-Host "  Done." -ForegroundColor Green

# ── 5. Get connection string ─────────────────────────────────────────────────
Write-Host "`nFetching connection string..." -ForegroundColor Cyan
$ConnStr = az storage account show-connection-string `
    --name $StorageAccount `
    --resource-group $ResourceGroup `
    --query connectionString `
    --output tsv
Write-Host "  Done." -ForegroundColor Green

# ── 6. Get static website URL ────────────────────────────────────────────────
$WebEndpoint = az storage account show `
    --name $StorageAccount `
    --resource-group $ResourceGroup `
    --query "primaryEndpoints.web" `
    --output tsv

# ── 7. AAD App Registration ──────────────────────────────────────────────────
Write-Host "`nCreating AAD App Registration '$AppName'..." -ForegroundColor Cyan
$App = az ad app create `
    --display-name $AppName `
    --sign-in-audience AzureADMyOrg `
    --web-redirect-uris "$($WebEndpoint).auth/login/aad/callback" `
    --output json | ConvertFrom-Json

$ClientId = $App.appId
$TenantId = $account.tenantId

Write-Host "  App registration created. Client ID: $ClientId" -ForegroundColor Green

# Create a client secret
$Secret = az ad app credential reset `
    --id $ClientId `
    --years 2 `
    --output json | ConvertFrom-Json
$ClientSecret = $Secret.password

# ── 8. Output ────────────────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  SETUP COMPLETE - copy these values into your .env file" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "PUBLISH_TO_AZURE=true"
Write-Host "AZURE_STORAGE_CONNECTION_STRING=$ConnStr"
Write-Host 'AZURE_STORAGE_CONTAINER=$web'
Write-Host ""
Write-Host "# For staticwebapp.config.json AAD auth:"
Write-Host "AZURE_CLIENT_ID=$ClientId"
Write-Host "AZURE_TENANT_ID=$TenantId"
Write-Host "AZURE_CLIENT_SECRET=$ClientSecret"
Write-Host ""
Write-Host "Dashboard URL: $WebEndpoint" -ForegroundColor Green
Write-Host ""
Write-Host 'NOTE: Also update staticwebapp.config.json with AZURE_CLIENT_ID and AZURE_TENANT_ID above.' -ForegroundColor Yellow
