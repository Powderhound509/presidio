# Variables
# Use the resource name shown in Azure, not the hashed login server prefix.
$ACR_NAME="acrjtlva"
$ANALYZER_SOURCE_IMAGE="ghcr.io/data-privacy-stack/presidio-analyzer"
$ANONYMIZER_SOURCE_IMAGE="ghcr.io/data-privacy-stack/presidio-anonymizer"
$REDACTOR_SOURCE_IMAGE="ghcr.io/data-privacy-stack/presidio-image-redactor"

$ANALYZER_TARGET_IMAGE="presidio/presidio-analyzer:latest"
$ANONYMIZER_TARGET_IMAGE="presidio/presidio-anonymizer:latest"
$REDACTOR_TARGET_IMAGE="presidio/presidio-image-redactor:latest"

# Log into Azure Gov if applicable
az cloud set --name AzureUSGovernment
    if ($LASTEXITCODE -ne 0) { throw "Failed to select Azure US Government." }
az login
    if ($LASTEXITCODE -ne 0) { throw "Azure login failed." }

# Resolve the full login server, including any domain name label scope hash.
$ACR_LOGIN_SERVER = az acr show --name $ACR_NAME --query loginServer --output tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ACR_LOGIN_SERVER)) {
    throw "Failed to resolve the ACR login server for '$ACR_NAME'."
    }

# Log into your ACR
az acr login --name $ACR_NAME
    if ($LASTEXITCODE -ne 0) { throw "ACR login failed." }

# Pull from GitHub Container Registry
docker pull $ANALYZER_SOURCE_IMAGE
    if ($LASTEXITCODE -ne 0) { throw "Failed to pull the analyzer image." }
docker pull $ANONYMIZER_SOURCE_IMAGE
    if ($LASTEXITCODE -ne 0) { throw "Failed to pull the anonymizer image." }
docker pull $REDACTOR_SOURCE_IMAGE
    if ($LASTEXITCODE -ne 0) { throw "Failed to pull the redactor image." }

# Tag it for your ACR
docker tag $ANALYZER_SOURCE_IMAGE "$ACR_LOGIN_SERVER/$ANALYZER_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to tag the analyzer image." }
docker tag $ANONYMIZER_SOURCE_IMAGE "$ACR_LOGIN_SERVER/$ANONYMIZER_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to tag the anonymizer image." }
docker tag $REDACTOR_SOURCE_IMAGE "$ACR_LOGIN_SERVER/$REDACTOR_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to tag the redactor image." }

# Push it
docker push "$ACR_LOGIN_SERVER/$ANALYZER_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to push the analyzer image." }
docker push "$ACR_LOGIN_SERVER/$ANONYMIZER_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to push the anonymizer image." }
docker push "$ACR_LOGIN_SERVER/$REDACTOR_TARGET_IMAGE"
    if ($LASTEXITCODE -ne 0) { throw "Failed to push the redactor image." }