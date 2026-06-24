# setup_azure.ps1
# Run this ONCE to provision Azure infrastructure for the ADO Dashboard.
# Prerequisites: az CLI installed, `az login` completed with your TR account.
#
# What this creates:
#   - Azure Static Web App (Standard tier) — hosts the dashboard
#   - AAD App Registration (single-tenant) — enforces TR SSO via /.auth/login/aad
#
# After running, copy the printed values into your GitHub repository secrets/variables.

$ErrorActionPreference = "Stop"

# ── Configuration ────────────────────────────────────────────────────────────
$ResourceGroup = "rg-ado-dashboard"
$Location      = "eastus2"          # supports Static Web Apps
$SwaName       = "swa-ado-dashboard"
$AppName       = "ado-dashboard-auth"

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

# ── 3. Static Web App ────────────────────────────────────────────────────────
Write-Host "`nCreating Static Web App '$SwaName'..." -ForegroundColor Cyan
# Standard tier is required for custom auth (AAD SSO).
# Free tier only supports managed auth with pre-built providers (no custom AAD tenant config).
$Swa = az staticwebapp create `
    --name $SwaName `
    --resource-group $ResourceGroup `
    --location $Location `
    --sku Standard `
    --output json | ConvertFrom-Json

$SwaHostname = $Swa.defaultHostname
$SwaUrl      = "https://$SwaHostname"
Write-Host "  Static Web App URL: $SwaUrl" -ForegroundColor Green

# ── 4. Retrieve deploy token ─────────────────────────────────────────────────
Write-Host "`nFetching deployment token..." -ForegroundColor Cyan
$DeployToken = az staticwebapp secrets list `
    --name $SwaName `
    --resource-group $ResourceGroup `
    --query "properties.apiKey" `
    --output tsv
Write-Host "  Done." -ForegroundColor Green

# ── 5. AAD App Registration ──────────────────────────────────────────────────
Write-Host "`nCreating AAD App Registration '$AppName'..." -ForegroundColor Cyan
$App = az ad app create `
    --display-name $AppName `
    --sign-in-audience AzureADMyOrg `
    --web-redirect-uris "$SwaUrl/.auth/login/aad/callback" `
    --output json | ConvertFrom-Json

$ClientId = $App.appId
$TenantId = $account.tenantId
Write-Host "  App registration created. Client ID: $ClientId" -ForegroundColor Green

# Create a client secret (2-year expiry)
$Secret = az ad app credential reset `
    --id $ClientId `
    --years 2 `
    --output json | ConvertFrom-Json
$ClientSecret = $Secret.password

# ── 6. Push app settings to Static Web App ───────────────────────────────────
# These become environment variables readable by the SWA auth runtime.
Write-Host "`nConfiguring app settings on Static Web App..." -ForegroundColor Cyan
az staticwebapp appsettings set `
    --name $SwaName `
    --resource-group $ResourceGroup `
    --setting-names `
        "AZURE_CLIENT_ID=$ClientId" `
        "AZURE_CLIENT_SECRET=$ClientSecret" `
    --output none
Write-Host "  Done." -ForegroundColor Green

# ── 7. Output ────────────────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  SETUP COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host 'Add these as GitHub repository secrets (Settings -> Secrets and variables -> Actions):' -ForegroundColor Yellow
Write-Host ""
Write-Host "  AZURE_STATIC_WEB_APPS_API_TOKEN  = $DeployToken"
Write-Host "  AZURE_CLIENT_SECRET              = $ClientSecret"
Write-Host ""
Write-Host "Add these as GitHub repository variables:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  AZURE_CLIENT_ID   = $ClientId"
Write-Host "  AZURE_TENANT_ID   = $TenantId"
Write-Host ""
Write-Host "Dashboard URL: $SwaUrl" -ForegroundColor Green
Write-Host ""
Write-Host "NOTE: staticwebapp.config.json already references AZURE_CLIENT_ID and AZURE_CLIENT_SECRET" -ForegroundColor Yellow
Write-Host '      as app setting names - no further changes needed there.' -ForegroundColor Yellow
Write-Host ""
Write-Host 'NOTE: Disable GitHub Pages for this repo (Settings -> Pages -> Source: None)' -ForegroundColor Yellow
Write-Host '      to prevent unauthenticated access via the old URL.' -ForegroundColor Yellow
