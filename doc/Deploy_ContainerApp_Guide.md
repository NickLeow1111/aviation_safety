# Deploy the Safety Intelligence Bot Container App — Private + Managed Identity

A step-by-step manual guide to deploy the agent **Container App** into its **own
resource group and VNet**, fronted by an **Application Gateway** (the only public
surface), reaching the existing **Foundry** backend (and, through it, **Synapse**)
entirely over **Private Endpoints / VNet peering** using **Microsoft Entra ID +
Managed Identity** — no API keys, no SAS, no SQL passwords.

```
            Internet
               │  (HTTPS, public)
        ┌──────▼───────┐
        │ App Gateway  │   WAF_v2, public IP   ── app RG / app VNet (snet-agw)
        └──────┬───────┘
               │  (private, internal LB)
        ┌──────▼───────┐
        │ Container App│   internal ingress    ── app RG / app VNet (snet-aca)
        │  (system MI) │
        └──────┬───────┘
               │  (Private Endpoint / peering, MI auth)
        ┌──────▼───────┐
        │   Foundry    │   srgsib-foundry      ── CAAS RG / srgsib-vnet (snet-pe)
        └──────┬───────┘
               │  (Private Endpoint, MI auth)
        ┌──────▼───────┐
        │   Synapse    │   caassynapse
        └──────────────┘
```

> The agent's local tools (`nl2sql`, `doc_search`) also call **Synapse SQL**,
> **AI Search**, and **ADLS** directly from the Container App. All of those
> already have Private Endpoints in `srgsib-vnet`, so this guide **peers** the new
> app VNet to `srgsib-vnet` and links the existing Private DNS zones — giving the
> app private reachability to every backend without duplicating endpoints.

---

## 0. Prerequisites

- Azure CLI ≥ 2.60, logged in: `az login`
- The Foundry stack is already deployed (`srgsib-foundry`, `srgsib-prj`,
  `srgsib-search`, `caassynapse`, `caasadlsv2`).
- You can resolve/admin the existing `CAAS` resource group and `srgsib-vnet`.
- A built container image for the agent (this guide builds it from
  [agent/Dockerfile](../Dockerfile)).

Enable the Container Apps CLI extension:

```bash
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.ContainerRegistry
```

---

## 1. Set working variables

```bash
# --- existing backend (CAAS) ---
SUBSCRIPTION_ID="57bbd325-81fb-4c5f-adee-489263236d32"
BACKEND_RG="CAAS"
BACKEND_VNET="srgsib-vnet"
FOUNDRY_NAME="srgsib-foundry"
FOUNDRY_PROJECT="srgsib-prj"
SEARCH_NAME="srgsib-search"
ADLS_NAME="caasadlsv2"
SYNAPSE_NAME="caassynapse"
SYNAPSE_SQLPOOL="caasedms"
SYNAPSE_SCHEMA="dbo"        # schema hosting the vw_SafetyIntel_* views (e.g. dbo or gold)

# --- new app environment ---
LOCATION="southeastasia"
APP_RG="CAAS-APP"
APP_VNET="srgsib-app-vnet"
APP_VNET_CIDR="10.60.0.0/22"
SNET_ACA="snet-aca";  SNET_ACA_CIDR="10.60.0.0/23"   # Container Apps (needs /23+)
SNET_AGW="snet-agw";  SNET_AGW_CIDR="10.60.2.0/24"   # Application Gateway
SNET_PE="snet-pe";    SNET_PE_CIDR="10.60.3.0/24"    # any app-local PEs

ACR_NAME="srgsibappacr$RANDOM"                        # must be globally unique
CAE_NAME="srgsib-app-cae"
APP_NAME="srgsib-app"
AGW_NAME="srgsib-app-agw"
AGW_PIP="srgsib-app-agw-pip"
LAW_NAME="srgsib-app-law"

az account set --subscription "$SUBSCRIPTION_ID"
```

---

## 2. Create the app resource group + VNet

```bash
az group create -n "$APP_RG" -l "$LOCATION"

az network vnet create \
  -g "$APP_RG" -n "$APP_VNET" -l "$LOCATION" \
  --address-prefixes "$APP_VNET_CIDR" \
  --subnet-name "$SNET_ACA" --subnet-prefixes "$SNET_ACA_CIDR"

# Container Apps infra subnet must be delegated to Microsoft.App/environments
az network vnet subnet update \
  -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_ACA" \
  --delegations Microsoft.App/environments

# App Gateway subnet (dedicated, no delegation)
az network vnet subnet create \
  -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_AGW" \
  --address-prefixes "$SNET_AGW_CIDR"

# Private Endpoint subnet (disable PE network policies)
az network vnet subnet create \
  -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_PE" \
  --address-prefixes "$SNET_PE_CIDR" \
  --disable-private-endpoint-network-policies true
```

---

## 3. Peer the app VNet to the backend VNet (private reachability)

This lets the Container App route to the Private Endpoints already living in
`srgsib-vnet` (Foundry, Search, ADLS, Synapse).

```bash
APP_VNET_ID=$(az network vnet show -g "$APP_RG" -n "$APP_VNET" --query id -o tsv)
BACKEND_VNET_ID=$(az network vnet show -g "$BACKEND_RG" -n "$BACKEND_VNET" --query id -o tsv)

az network vnet peering create \
  -g "$APP_RG" -n app-to-backend --vnet-name "$APP_VNET" \
  --remote-vnet "$BACKEND_VNET_ID" \
  --allow-vnet-access --allow-forwarded-traffic

az network vnet peering create \
  -g "$BACKEND_RG" -n backend-to-app --vnet-name "$BACKEND_VNET" \
  --remote-vnet "$APP_VNET_ID" \
  --allow-vnet-access --allow-forwarded-traffic
```

> Verify both peerings show `peeringState = Connected`:
> `az network vnet peering list -g "$APP_RG" --vnet-name "$APP_VNET" -o table`

---

## 4. Link the existing Private DNS zones to the app VNet

Without this, the app resolves backend hostnames to public IPs (which are
blocked). Link each `privatelink.*` zone that already exists in `CAAS`.

```bash
ZONES=(
  privatelink.services.ai.azure.com
  privatelink.openai.azure.com
  privatelink.cognitiveservices.azure.com
  privatelink.search.windows.net
  privatelink.blob.core.windows.net
  privatelink.dfs.core.windows.net
  privatelink.sql.azuresynapse.net
  privatelink.dev.azuresynapse.net
)

for z in "${ZONES[@]}"; do
  az network private-dns link vnet create \
    -g "$BACKEND_RG" -z "$z" \
    -n "${APP_VNET}-link" -v "$APP_VNET_ID" -e false
done
```

---

## 5. Create a private Azure Container Registry (in the app RG)

```bash
az acr create -g "$APP_RG" -n "$ACR_NAME" -l "$LOCATION" \
  --sku Premium --admin-enabled false \
  --public-network-enabled true          # temporary, for the first image push

ACR_ID=$(az acr show -g "$APP_RG" -n "$ACR_NAME" --query id -o tsv)
ACR_LOGIN_SERVER=$(az acr show -g "$APP_RG" -n "$ACR_NAME" --query loginServer -o tsv)
```

### Build & push the image

```bash
# Build inside ACR (no local Docker needed). Run from the repo root.
az acr build -r "$ACR_NAME" -t "safety-bot:v1" -f agent/Dockerfile agent
```

### Lock the registry down (private endpoint + disable public access)

```bash
az network private-endpoint create \
  -g "$APP_RG" -n "${ACR_NAME}-pe" -l "$LOCATION" \
  --vnet-name "$APP_VNET" --subnet "$SNET_PE" \
  --private-connection-resource-id "$ACR_ID" \
  --group-id registry --connection-name "${ACR_NAME}-conn"

az network private-dns zone create -g "$APP_RG" -n privatelink.azurecr.io
az network private-dns link vnet create \
  -g "$APP_RG" -z privatelink.azurecr.io \
  -n "${APP_VNET}-link" -v "$APP_VNET_ID" -e false
az network private-endpoint dns-zone-group create \
  -g "$APP_RG" --endpoint-name "${ACR_NAME}-pe" -n default \
  --private-dns-zone privatelink.azurecr.io --zone-name registry

# Now disable public access (pulls happen via PE + managed identity)
az acr update -n "$ACR_NAME" --public-network-enabled false
```

> If `az acr build` must run after lock-down, use an ACR **dedicated agent pool**
> in the VNet or build from a jumpbox VM with line-of-sight to the PE.

---

## 6. Create the Container Apps environment (VNet-injected, internal)

```bash
# Log Analytics for the environment (keyless)
az monitor log-analytics workspace create \
  -g "$APP_RG" -n "$LAW_NAME" -l "$LOCATION"
LAW_ID=$(az monitor log-analytics workspace show -g "$APP_RG" -n "$LAW_NAME" --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$APP_RG" -n "$LAW_NAME" --query primarySharedKey -o tsv)
ACA_SUBNET_ID=$(az network vnet subnet show -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_ACA" --query id -o tsv)

az containerapp env create \
  -g "$APP_RG" -n "$CAE_NAME" -l "$LOCATION" \
  --infrastructure-subnet-resource-id "$ACA_SUBNET_ID" \
  --internal-only true \
  --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY"
```

> `--internal-only true` gives the environment an **internal** load balancer
> (private IP only). Public access comes solely through App Gateway in Step 9.

---

## 7. Deploy the Container App (managed identity, external ingress)

> **IMPORTANT — use `--ingress external`, not `internal`.** The environment is
> already **internal-only** (Step 6, `--internal-only true`), so the app has **no
> public exposure** either way — its FQDN resolves only to the env's private load
> balancer. The `external` flag controls whether the app is **published on the
> environment's load-balancer frontend** (the static IP that App Gateway targets).
> With `--ingress internal` the app is reachable **only via the in-cluster service
> mesh** (`*.internal.<envdomain>` → a non-routable `100.x` mesh IP), so App
> Gateway gets a **404 from Envoy** and backend health never goes Healthy. With
> `--ingress external` on an internal env, the app is published at
> `<app>.<envdomain>` on the env's private LB static IP — still private, and now
> reachable by App Gateway.

```bash
FOUNDRY_PROJECT_ENDPOINT="https://${FOUNDRY_NAME}.services.ai.azure.com/api/projects/${FOUNDRY_PROJECT}"

az containerapp create \
  -g "$APP_RG" -n "$APP_NAME" \
  --environment "$CAE_NAME" \
  --image "${ACR_LOGIN_SERVER}/safety-bot:v1" \
  --registry-server "$ACR_LOGIN_SERVER" \
  --registry-identity system \
  --system-assigned \
  --ingress external --target-port 7700 --transport auto \
  --min-replicas 1 --max-replicas 3 \
  --cpu 0.5 --memory 1Gi \
  --env-vars \
    AZURE_AI_PROJECT_ENDPOINT="$FOUNDRY_PROJECT_ENDPOINT" \
    AZURE_OPENAI_ENDPOINT="https://${FOUNDRY_NAME}.openai.azure.com" \
    AZURE_OPENAI_DEPLOYMENT="gpt-5.2" \
    AZURE_OPENAI_EMBED_DEPLOYMENT="text-embedding-3-large" \
    AZURE_OPENAI_API_VERSION="2025-04-01-preview" \
    SEARCH_ENDPOINT="https://${SEARCH_NAME}.search.windows.net" \
    SEARCH_INDEX="safety-docs" \
    SYNAPSE_SQL_SERVER="${SYNAPSE_NAME}.sql.azuresynapse.net" \
    SYNAPSE_SQL_DATABASE="${SYNAPSE_SQLPOOL}" \
    SYNAPSE_SQL_SCHEMA="${SYNAPSE_SCHEMA}" \
    ADLS_ACCOUNT="$ADLS_NAME" \
    ADLS_DOCS_FILESYSTEM="docs"

APP_MI=$(az containerapp show -g "$APP_RG" -n "$APP_NAME" --query identity.principalId -o tsv)
echo "Container App MI principalId: $APP_MI"
```

> `--target-port 7700` matches the container's listening port (`uvicorn ... --port
> 7700` / `EXPOSE 7700` in [agent/Dockerfile](../Dockerfile)). App Gateway still
> talks to the Container App's internal FQDN over HTTPS 443; ingress maps that to
> 7700 inside the container.

---

## 8. Grant the Container App managed identity its RBAC (no keys)

```bash
FOUNDRY_ID=$(az cognitiveservices account show -g "$BACKEND_RG" -n "$FOUNDRY_NAME" --query id -o tsv)
SEARCH_ID=$(az search service show -g "$BACKEND_RG" -n "$SEARCH_NAME" --query id -o tsv)
ADLS_ID=$(az storage account show -g "$BACKEND_RG" -n "$ADLS_NAME" --query id -o tsv)

# Foundry Agent Service caller + connection read + direct AOAI/embeddings
# NOTE: In this tenant the newer roles "Azure AI User" / "Azure AI Developer" are
# a STALE generation and do NOT grant the data action the app needs to read the
# project's connections (Microsoft.CognitiveServices/accounts/AIServices/connections/read).
# Use "Foundry User" (data actions = Microsoft.CognitiveServices/*), which covers
# connection read, agent run, and inference. Verified working for the agent script.
az role assignment create --assignee-object-id "$APP_MI" --assignee-principal-type ServicePrincipal \
  --role "Foundry User" --scope "$FOUNDRY_ID"
az role assignment create --assignee-object-id "$APP_MI" --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope "$FOUNDRY_ID"

# AI Search (read indexes for doc_search)
az role assignment create --assignee-object-id "$APP_MI" --assignee-principal-type ServicePrincipal \
  --role "Search Index Data Reader" --scope "$SEARCH_ID"

# ADLS (read regulatory docs)
az role assignment create --assignee-object-id "$APP_MI" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" --scope "$ADLS_ID"

# ACR pull (image)
az role assignment create --assignee-object-id "$APP_MI" --assignee-principal-type ServicePrincipal \
  --role "AcrPull" --scope "$ACR_ID"
```

---

## 9. Grant the managed identity access to Synapse SQL (for nl2sql)

Connect to the Synapse Dedicated SQL pool **as an Entra admin** (via Synapse
Studio over its Private Endpoint, or a jumpbox with `sqlcmd`) and run:

```sql
CREATE USER [srgsib-app] FROM EXTERNAL PROVIDER;
-- Dedicated SQL pool does NOT support "ALTER ROLE ... ADD MEMBER";
-- use the legacy stored procedure instead.
EXEC sp_addrolemember 'db_datareader', 'srgsib-app';
-- grant EXECUTE on specific procs/schemas if your nl2sql tool needs them
```

> `[srgsib-app]` is the Container App's name (its managed identity). Adjust if you
> renamed `APP_NAME`.

### Scope the grant to a schema (least privilege)

`db_datareader` grants `SELECT` on **every** table/view in the database. To follow
least privilege, drop the role membership and grant `SELECT` only on the schema
that holds the SafetyIntel objects. All tables and views in this repo are created
in the **`dbo`** schema (e.g. `dbo.DM_TBL_SRG_*`, `dbo.vw_SafetyIntel_*`), so:

```sql
CREATE USER [srgsib-app] FROM EXTERNAL PROVIDER;

-- Schema-scoped read access (covers all current + future objects in the schema)
GRANT SELECT ON SCHEMA::dbo TO [srgsib-app];

-- If nl2sql calls any stored procedures/functions, also grant EXECUTE on the schema
GRANT EXECUTE ON SCHEMA::dbo TO [srgsib-app];
```

> A schema-level `GRANT SELECT`/`GRANT EXECUTE` automatically applies to objects
> added to that schema later, so you don't re-run it when new views ship. If you
> later split objects into a dedicated schema (e.g. `safetyintel`), create that
> schema and repeat the grant against `SCHEMA::safetyintel` instead.

If you already ran `sp_addrolemember 'db_datareader', ...` and want to tighten it,
revoke the broad role first, then apply the schema grant:

```sql
EXEC sp_droprolemember 'db_datareader', 'srgsib-app';
GRANT SELECT ON SCHEMA::dbo TO [srgsib-app];
```

To grant only specific tables/views instead of the whole schema, name each object:

```sql
GRANT SELECT ON OBJECT::dbo.vw_SafetyIntel_AMO TO [srgsib-app];
GRANT SELECT ON OBJECT::dbo.vw_SafetyIntel_Occurrences TO [srgsib-app];
-- ...repeat per view the agent is allowed to read
```

Verify what the user can actually see:

```sql
-- Effective permissions for the managed-identity user
SELECT * FROM sys.database_permissions dp
JOIN sys.database_principals pr ON dp.grantee_principal_id = pr.principal_id
WHERE pr.name = 'srgsib-app';
```

### Non-`dbo` schema (e.g. `gold`)

If the SafetyIntel tables/views live in a schema other than `dbo`, set the app's
`SYNAPSE_SQL_SCHEMA` env var to that schema (Step 1 / Step 7). The `nl2sql` tool
then rewrites every `vw_SafetyIntel_*` reference to `<schema>.vw_SafetyIntel_*`
before executing, so queries resolve regardless of the user's default schema.

Grant against that schema and (optionally) set it as the user's default:

```sql
CREATE USER [srgsib-app] FROM EXTERNAL PROVIDER;
GRANT SELECT  ON SCHEMA::gold TO [srgsib-app];
GRANT EXECUTE ON SCHEMA::gold TO [srgsib-app];   -- only if nl2sql calls procs
ALTER USER [srgsib-app] WITH DEFAULT_SCHEMA = gold;  -- optional; queries are already schema-qualified
```

> To move existing `dbo` objects into `gold`, use the migration script
> [SQL/migrate_dbo_to_gold.sql](../SQL/migrate_dbo_to_gold.sql), then create the
> gold views with [SQL/vw_SafetyIntel_Views_Gold.sql](../SQL/vw_SafetyIntel_Views_Gold.sql).

---

## 9b. Create the environment's private DNS zone (so App Gateway can resolve the app)

App Gateway resolves the backend **FQDN** via DNS. The Container Apps environment
default domain (`*.azurecontainerapps.io`) is **not** registered in your VNet, so
without this zone the backend health shows **`Unknown`** (DNS failure). Create a
private DNS zone for the env default domain, point a wildcard `*` (and `@`) record
at the env's **static IP**, and link it to the app VNet.

```bash
# Default domain + internal LB static IP of the Container Apps environment
ENV_DOMAIN=$(az containerapp env show -g "$APP_RG" -n "$CAE_NAME" --query properties.defaultDomain -o tsv)
ENV_IP=$(az containerapp env show -g "$APP_RG" -n "$CAE_NAME" --query properties.staticIp -o tsv)
echo "env domain=$ENV_DOMAIN  staticIp=$ENV_IP"
APP_VNET_ID=$(az network vnet show -g "$APP_RG" -n "$APP_VNET" --query id -o tsv)

# Private DNS zone = env default domain
az network private-dns zone create -g "$APP_RG" -n "$ENV_DOMAIN"

# Link it to the app VNet (so the AGW in this VNet resolves it)
az network private-dns link vnet create \
  -g "$APP_RG" -z "$ENV_DOMAIN" -n "${APP_VNET}-link" \
  -v "$APP_VNET_ID" -e false

# Wildcard + apex A records -> env static (internal LB) IP.
# '*' matches <app>.<envdomain> (the external-ingress FQDN App Gateway targets).
az network private-dns record-set a add-record -g "$APP_RG" -z "$ENV_DOMAIN" -n "*" -a "$ENV_IP"
az network private-dns record-set a add-record -g "$APP_RG" -z "$ENV_DOMAIN" -n "@" -a "$ENV_IP"
```

> If the app ingress were left as `internal` (Step 7), its FQDN would be
> `<app>.internal.<envdomain>` and you'd need a `*.internal` record **and** it
> still wouldn't work, because internal-ingress apps aren't published on the env
> LB. Keeping the app ingress **external** (private, on an internal env) is what
> makes the `*` record + LB static IP path work end-to-end.

---

## 10. Deploy the Application Gateway (the only public surface)

```bash
AGW_SUBNET_ID=$(az network vnet subnet show -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_AGW" --query id -o tsv)

# Public IP (Standard, static) — the single internet entry point
az network public-ip create \
  -g "$APP_RG" -n "$AGW_PIP" -l "$LOCATION" \
  --sku Standard --allocation-method Static

# Container App FQDN = App Gateway backend target (non-.internal because the app
# ingress is external; published on the env's private LB static IP)
APP_FQDN=$(az containerapp show -g "$APP_RG" -n "$APP_NAME" --query properties.configuration.ingress.fqdn -o tsv)
echo "Backend FQDN: $APP_FQDN"   # e.g. srgsib-app.<envdomain> (NOT *.internal.*)

# WAF policy is REQUIRED for the WAF_v2 SKU. Create it first; it ships with the
# OWASP 3.2 managed rule set (Detection mode by default — switched to Prevention
# in the lockdown step below).
az network application-gateway waf-policy create \
  -g "$APP_RG" -n "${AGW_NAME}-waf" -l "$LOCATION"

az network application-gateway create \
  -g "$APP_RG" -n "$AGW_NAME" -l "$LOCATION" \
  --sku WAF_v2 --capacity 2 \
  --vnet-name "$APP_VNET" --subnet "$SNET_AGW" \
  --public-ip-address "$AGW_PIP" \
  --servers "$APP_FQDN" \
  --http-settings-port 443 --http-settings-protocol Https \
  --frontend-port 80 \
  --waf-policy "${AGW_NAME}-waf" \
  --priority 100
```

> **Why frontend port 80 here?** `az ... application-gateway create` makes the
> frontend listener **HTTP** unless you also pass a certificate. An HTTP listener
> on 443 is rejected (`ApplicationGatewayPortNotValidForProtocol`). So we bootstrap
> the gateway on HTTP/80 and add the real **HTTPS/443** listener + cert in the
> friendly-domain step below. (`--http-settings-*` is the *backend* leg to the
> Container App and stays HTTPS/443.) If you already have the PFX, you can instead
> create with `--frontend-port 443 --cert-file ./your.pfx --cert-password "<pwd>"`.

### Backend health probe + host header (point to the internal FQDN)

```bash
# Custom probe using the backend FQDN as host.
# Use /healthz (always returns 200) — path / returns 404 unless the SPA static
# files are bundled, which would mark the backend Unhealthy.
az network application-gateway probe create \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n aca-probe \
  --protocol Https --host "$APP_FQDN" --path /healthz \
  --interval 30 --timeout 30 --threshold 3

az network application-gateway http-settings update \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n appGatewayBackendHttpSettings \
  --host-name "$APP_FQDN" --probe aca-probe \
  --protocol Https --port 443
```

> **TLS:** add an HTTPS listener with your own certificate
> (`az network application-gateway ssl-cert create ...`) and bind the rule to it.
> For a quick test you can use an HTTP listener on port 80 instead, but production
> should terminate TLS at the gateway. The Container App's internal ingress already
> serves HTTPS on its `*.azurecontainerapps.io` FQDN, which Azure DNS resolves to
> the internal LB IP from inside the app VNet.

### (Optional) Give it a friendly public domain (e.g. `srgsib-app.caasapp.gov.sg`)

The friendly name lives on the **App Gateway** (the public surface), not on the
Container App. Users hit `https://srgsib-app.caasapp.gov.sg`; the gateway still
forwards privately to the internal container app FQDN. Three pieces: a **DNS
record**, a **TLS cert**, and an **HTTPS listener** bound to that hostname.

```bash
FRIENDLY_FQDN="srgsib-app.caasapp.gov.sg"
DNS_RG="<rg-hosting-the-dns-zone>"        # only if the zone is in Azure DNS
DNS_ZONE="caasapp.gov.sg"

# 1. DNS: point the friendly name at the App Gateway public IP.
AGW_IP=$(az network public-ip show -g "$APP_RG" -n "$AGW_PIP" --query ipAddress -o tsv)
echo "Create an A record: $FRIENDLY_FQDN -> $AGW_IP"
# If the zone is hosted in Azure DNS:
az network dns record-set a add-record \
  -g "$DNS_RG" -z "$DNS_ZONE" -n srgsib-app -a "$AGW_IP"
# Otherwise create the A record in your gov.sg DNS provider manually.

# 2. Upload the TLS cert for that hostname (PFX with private key).
az network application-gateway ssl-cert create \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n srgsib-cert \
  --cert-file ./srgsib-app.caasapp.gov.sg.pfx --cert-password "<pfx-password>"

# 3. Add a 443 frontend port (the gateway was bootstrapped on 80), then an
#    HTTPS listener bound to the cert + friendly hostname, routed to the
#    existing backend pool (the internal container app).
az network application-gateway frontend-port create \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n https-port --port 443

POOL=$(az network application-gateway address-pool list \
  -g "$APP_RG" --gateway-name "$AGW_NAME" --query "[0].name" -o tsv)

az network application-gateway http-listener create \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n friendly-listener \
  --frontend-port https-port --ssl-cert srgsib-cert \
  --host-name "$FRIENDLY_FQDN"

az network application-gateway rule create \
  -g "$APP_RG" --gateway-name "$AGW_NAME" -n friendly-rule \
  --http-listener friendly-listener \
  --address-pool "$POOL" --http-settings appGatewayBackendHttpSettings \
  --priority 90
```

> The backend HTTP settings still use `--host-name "$APP_FQDN"` (the internal
> ACA FQDN) so the probe and SNI to the Container App stay valid — only the
> *public-facing* listener uses the friendly name. `gov.sg` domains must be
> registered/delegated through the proper process, and the cert should be issued
> by a trusted/government CA.



```bash
# Set the attached WAF policy to Prevention mode (WAF_v2 uses a policy, not the
# legacy inline waf-config). The OWASP 3.2 managed rule set is already attached.
az network application-gateway waf-policy policy-setting update \
  -g "$APP_RG" --policy-name "${AGW_NAME}-waf" \
  --state Enabled --mode Prevention

# Restrict the AGW subnet inbound to HTTPS + the GatewayManager health ports
az network nsg create -g "$APP_RG" -n "${SNET_AGW}-nsg" -l "$LOCATION"
az network nsg rule create -g "$APP_RG" --nsg-name "${SNET_AGW}-nsg" -n allow-https \
  --priority 100 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes Internet --destination-port-ranges 443
# TEMPORARY: the gateway is bootstrapped on an HTTP/80 listener (no cert yet), so
# allow 80 to test via http://<AGW_PUBLIC_IP>/. Remove this rule once you add the
# HTTPS/443 listener + cert (friendly-domain step above).
az network nsg rule create -g "$APP_RG" --nsg-name "${SNET_AGW}-nsg" -n allow-http \
  --priority 105 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes Internet --destination-port-ranges 80
az network nsg rule create -g "$APP_RG" --nsg-name "${SNET_AGW}-nsg" -n allow-gwmgr \
  --priority 110 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes GatewayManager --destination-port-ranges 65200-65535
az network vnet subnet update -g "$APP_RG" --vnet-name "$APP_VNET" -n "$SNET_AGW" \
  --network-security-group "${SNET_AGW}-nsg"
```

---

## 11. Register the Foundry agent

From a host with line-of-sight to the Foundry Private Endpoint (jumpbox in the
VNet, or after the peering above from the app subnet):

```bash
source .venv/bin/activate
python agent/scripts/create_foundry_agent.py   # uses AZURE_AI_PROJECT_ENDPOINT
```

---

## 12. Load RAG data (one-time)

```bash
# Upload regulatory PDFs to ADLS 'docs' container (AAD auth, no keys)
az storage fs create -n docs --account-name "$ADLS_NAME" --auth-mode login
az storage fs directory upload -f docs --account-name "$ADLS_NAME" \
  --auth-mode login -s ./regulatory-docs -d / --recursive

# Build the 'safety-docs' AI Search index from inside the VNet
# (run your index build script with line-of-sight to the Search PE)
```

---

## 13. Validate end-to-end

```bash
# 1. Gateway is up and healthy.
# NOTE: `-o table` prints NOTHING for backend-health (nested output the table
# formatter can't flatten). Use a --query projection instead:
az network application-gateway show-backend-health \
  -g "$APP_RG" -n "$AGW_NAME" \
  --query "backendAddressPools[].backendHttpSettingsCollection[].servers[].{address:address, health:health, reason:healthProbeLog}" \
  -o json
# Expect health = "Healthy".

# 2. Public entry point
AGW_IP=$(az network public-ip show -g "$APP_RG" -n "$AGW_PIP" --query ipAddress -o tsv)
curl -sk "https://$AGW_IP/healthz"     # or your app's health path

# 3. App logs
az containerapp logs show -g "$APP_RG" -n "$APP_NAME" --tail 50
```

Confirm the lockdown:

- Container App ingress = `internal` (no public FQDN reachable from internet).
- App Gateway public IP is the **only** internet-facing endpoint.
- No API keys / connection strings in env vars — only endpoints + deployment names.
- All backend calls (Foundry, Search, ADLS, Synapse) resolve to `privatelink`
  addresses and authenticate with the Container App's managed identity.

---

## Resource summary

| Resource | Name | RG | Public? |
|---|---|---|---|
| Resource group | `CAAS-APP` | — | — |
| VNet | `srgsib-app-vnet` (`10.60.0.0/22`) | CAAS-APP | No |
| Container Apps env | `srgsib-app-cae` (internal) | CAAS-APP | No |
| Container App | `srgsib-app` (system MI) | CAAS-APP | No |
| Container Registry | `srgsibappacr*` (Premium, PE) | CAAS-APP | No |
| Application Gateway | `srgsib-app-agw` (WAF_v2) | CAAS-APP | **Yes (only)** |
| Public IP | `srgsib-app-agw-pip` | CAAS-APP | **Yes** |
| Log Analytics | `srgsib-app-law` | CAAS-APP | No |
| VNet peering | app-vnet ↔ srgsib-vnet | both | No |
| Foundry / Search / Synapse / ADLS | existing | CAAS | No |
