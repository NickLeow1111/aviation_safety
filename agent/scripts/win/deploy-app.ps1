<#
.SYNOPSIS
    Deploy the Safety Intelligence Bot to Azure Container Apps and register the
    Foundry agent. PowerShell equivalent of the focused subset of
    doc/Deploy_ContainerApp_Guide.md (image build + Container App + agent only).

.DESCRIPTION
    This script assumes the surrounding infrastructure (resource group, VNet,
    peering, private DNS, ACR, Container Apps environment, RBAC) already exists.
    It performs only the two app-level tasks:

      1. Build & push the container image into the existing ACR (az acr build).
      2. Create or update the Container App (system-assigned managed identity,
         external ingress on the internal env).
      3. Register / update the Foundry v2 PromptAgent
         (agent/scripts/create_foundry_agent.py).

    No keys are used anywhere - the Container App authenticates to Foundry,
    Search, ADLS and Synapse with its system-assigned managed identity.

.PARAMETER SkipImageBuild
    Skip the "az acr build" step (reuse the existing image tag).

.PARAMETER SkipAgent
    Skip the Foundry agent registration step.

.EXAMPLE
    pwsh ./deploy-app.ps1

.EXAMPLE
    pwsh ./deploy-app.ps1 -ImageTag v2 -SkipAgent

.NOTES
    Prereqs:
      - Azure CLI >= 2.60, logged in (az login) with rights on both RGs.
      - containerapp CLI extension (the script installs/updates it).
      - Python 3.11+ with agent/requirements.txt installed in a venv, for the
        agent registration step. Run that step from a host with private
        line-of-sight to the Foundry private endpoint (e.g. a Bastion jumpbox).
#>

[CmdletBinding()]
param(
    # --- existing backend (CAAS) ---
    [string]$SubscriptionId   = "57bbd325-81fb-4c5f-adee-489263236d32",
    [string]$BackendRg        = "CAAS",
    [string]$FoundryName      = "srgsib-foundry",
    [string]$FoundryProject   = "srgsib-prj",
    [string]$SearchName       = "srgsib-search",
    [string]$AdlsName         = "caasadlsv2",
    [string]$SynapseName      = "caassynapse",
    [string]$SynapseSqlPool   = "caasedms",
    [string]$SynapseSchema    = "dbo",

    # --- existing app environment ---
    [string]$AppRg            = "CAAS-APP",
    [string]$CaeName          = "srgsib-app-cae",
    [string]$AppName          = "srgsib-app",
    [string]$AcrName          = "",          # auto-detected from $AppRg if empty
    [string]$ImageTag         = "v1",
    [string]$ImageRepo        = "safety-bot",

    # --- model / index config (env vars on the Container App) ---
    [string]$OpenAiDeployment      = "gpt-5.2",
    [string]$OpenAiEmbedDeployment = "text-embedding-3-large",
    [string]$OpenAiApiVersion      = "2025-04-01-preview",
    [string]$SearchIndex           = "safety-docs",
    [string]$AgentName             = "safety-intelligence-bot",

    [switch]$SkipImageBuild,
    [switch]$SkipAgent
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# PowerShell 7.4+ treats ANY non-zero native exit code as a terminating error when
# $ErrorActionPreference = 'Stop'. We rely on explicit $LASTEXITCODE checks in
# Invoke-Az instead, so disable that behavior. This also keeps benign az stderr
# warnings (e.g. "behavior altered by extension: containerapp") from aborting the
# run, and lets the 'az containerapp show' existence probe fail gracefully.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# Reduce az log noise (only errors); keeps the transcript readable on Windows.
$env:AZURE_CORE_ONLY_SHOW_ERRORS = "true"

# Resolve repo paths relative to this script: scripts/win -> scripts -> agent -> repo
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path          # ...\agent
$RepoRoot  = (Resolve-Path (Join-Path $AgentRoot "..")).Path             # repo root

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ">> $Message" -ForegroundColor Cyan
}

function Invoke-Az {
    # Run an az command (string args) and throw on non-zero exit.
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AzArgs)
    & az @AzArgs
    if ($LASTEXITCODE -ne 0) {
        throw "az $($AzArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

# -----------------------------------------------------------------------------
# 0. Pre-flight
# -----------------------------------------------------------------------------
Write-Step "Checking Azure CLI"
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI (az) not found on PATH. Install it and run 'az login' first."
}

Write-Step "Ensuring containerapp extension + providers are registered"
Invoke-Az extension add --name containerapp --upgrade --only-show-errors
Invoke-Az provider register --namespace Microsoft.App --only-show-errors
Invoke-Az provider register --namespace Microsoft.OperationalInsights --only-show-errors

Write-Step "Setting subscription: $SubscriptionId"
Invoke-Az account set --subscription $SubscriptionId

# -----------------------------------------------------------------------------
# 1. Resolve the ACR
# -----------------------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($AcrName)) {
    Write-Step "Auto-detecting ACR in resource group $AppRg"
    $AcrName = az acr list -g $AppRg --query "[?starts_with(name,'srgsibappacr')].name | [0]" -o tsv
    if ([string]::IsNullOrWhiteSpace($AcrName)) {
        $AcrName = az acr list -g $AppRg --query "[0].name" -o tsv
    }
    if ([string]::IsNullOrWhiteSpace($AcrName)) {
        throw "No Azure Container Registry found in '$AppRg'. Pass -AcrName explicitly."
    }
}
$AcrLoginServer = az acr show -g $AppRg -n $AcrName --query loginServer -o tsv
if ([string]::IsNullOrWhiteSpace($AcrLoginServer)) {
    throw "Could not resolve login server for ACR '$AcrName'."
}
$Image = "$AcrLoginServer/$($ImageRepo):$ImageTag"
Write-Host "   ACR           : $AcrName ($AcrLoginServer)"
Write-Host "   Image         : $Image"

# -----------------------------------------------------------------------------
# 2. Build & push the image (inside ACR - no local Docker needed)
# -----------------------------------------------------------------------------
if ($SkipImageBuild) {
    Write-Step "Skipping image build (-SkipImageBuild); reusing $Image"
}
else {
    Write-Step "Building & pushing image with az acr build"
    $DockerContext = Join-Path $RepoRoot "agent"
    $Dockerfile    = Join-Path $DockerContext "Dockerfile"
    Invoke-Az acr build -r $AcrName -t "$($ImageRepo):$ImageTag" -f $Dockerfile $DockerContext
}

# -----------------------------------------------------------------------------
# 3. Create or update the Container App
# -----------------------------------------------------------------------------
$FoundryProjectEndpoint = "https://$FoundryName.services.ai.azure.com/api/projects/$FoundryProject"

$envVars = @(
    "AZURE_AI_PROJECT_ENDPOINT=$FoundryProjectEndpoint",
    "AZURE_OPENAI_ENDPOINT=https://$FoundryName.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT=$OpenAiDeployment",
    "AZURE_OPENAI_EMBED_DEPLOYMENT=$OpenAiEmbedDeployment",
    "AZURE_OPENAI_API_VERSION=$OpenAiApiVersion",
    "SEARCH_ENDPOINT=https://$SearchName.search.windows.net",
    "SEARCH_INDEX=$SearchIndex",
    "SYNAPSE_SQL_SERVER=$SynapseName.sql.azuresynapse.net",
    "SYNAPSE_SQL_DATABASE=$SynapseSqlPool",
    "SYNAPSE_SQL_SCHEMA=$SynapseSchema",
    "ADLS_ACCOUNT=$AdlsName",
    "ADLS_DOCS_FILESYSTEM=docs",
    "FOUNDRY_AGENT_NAME=$AgentName"
)

# Use 'list' (not 'show'): it returns an empty result + exit 0 when the app is
# absent, instead of 'show' which errors with ResourceNotFound and prints to
# stderr. This keeps the existence probe quiet and non-fatal.
$appExists = az containerapp list -g $AppRg --query "[?name=='$AppName'].name | [0]" -o tsv 2>$null
$global:LASTEXITCODE = 0

if ([string]::IsNullOrWhiteSpace($appExists)) {
    Write-Step "Creating Container App: $AppName"
    Invoke-Az containerapp create `
        -g $AppRg -n $AppName `
        --environment $CaeName `
        --image $Image `
        --registry-server $AcrLoginServer `
        --registry-identity system `
        --system-assigned `
        --ingress external --target-port 7700 --transport auto `
        --min-replicas 1 --max-replicas 3 `
        --cpu 0.5 --memory 1Gi `
        --env-vars @envVars
}
else {
    Write-Step "Updating existing Container App: $AppName"
    Invoke-Az containerapp update `
        -g $AppRg -n $AppName `
        --image $Image `
        --set-env-vars @envVars
}

$AppMi = az containerapp show -g $AppRg -n $AppName --query identity.principalId -o tsv
$AppFqdn = az containerapp show -g $AppRg -n $AppName --query properties.configuration.ingress.fqdn -o tsv
Write-Host "   Container App MI principalId : $AppMi"
Write-Host "   Container App FQDN           : $AppFqdn"

# -----------------------------------------------------------------------------
# 4. Register the Foundry agent
# -----------------------------------------------------------------------------
if ($SkipAgent) {
    Write-Step "Skipping Foundry agent registration (-SkipAgent)"
}
else {
    Write-Step "Registering Foundry agent: $AgentName"
    Write-Host "   NOTE: Foundry has public network access disabled. Run this step from" -ForegroundColor Yellow
    Write-Host "   a host with private line-of-sight to the Foundry endpoint (e.g. Bastion)." -ForegroundColor Yellow

    # Pick a python interpreter: prefer the repo .venv, else system python.
    $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        $python = $venvPy
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $python = "python"
    }
    elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
        $python = "python3"
    }
    else {
        throw "No python interpreter found. Create a venv and install agent/requirements.txt first."
    }

    # The agent script reads these from the environment (or agent/.env.foundry).
    $env:AZURE_AI_PROJECT_ENDPOINT = $FoundryProjectEndpoint
    $env:AZURE_OPENAI_DEPLOYMENT   = $OpenAiDeployment
    $env:FOUNDRY_AGENT_NAME        = $AgentName
    $env:SEARCH_INDEX              = $SearchIndex

    $agentScript = Join-Path $AgentRoot "scripts\create_foundry_agent.py"
    & $python $agentScript
    if ($LASTEXITCODE -ne 0) {
        throw "Foundry agent registration failed with exit code $LASTEXITCODE"
    }
}

Write-Step "Done"
Write-Host "Container App : $AppName  ($AppFqdn)" -ForegroundColor Green
Write-Host "Foundry agent : $AgentName" -ForegroundColor Green
