az network application-gateway start -g CAAS-APP -n srgsib-app-agw

Building container image at ACR at Cloud Shell

git clone https://github.com/alvintai78/aviation_safety
grep -n "7700" agent/Dockerfile 
az account set --subscription 57bbd325-81fb-4c5f-adee-489263236d32
az account show --query "user.name" -o tsv

az acr update -n srgsibappacr32226 -g CAAS-APP --public-network-enabled true

cd aviation_safety/
az acr build -r srgsibappacr32226 -t "safety-bot:v3" -f agent/Dockerfile agent

THen make sure the port number is matched at ingress and at health probes