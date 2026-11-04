param(
    [Parameter(Mandatory)][string]$Worksheet,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$data = Get-Content -LiteralPath $Worksheet -Raw | ConvertFrom-Json
$cloud = az cloud show --query name -o tsv
if ($LASTEXITCODE -ne 0 -or $cloud -ne 'AzureUSGovernment') {
    throw 'Select the approved AzureUSGovernment operator context first.'
}
foreach ($binding in $data.rbac) {
    if ($binding.scope -notmatch '^/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/.+') {
        throw 'Role assignments require explicit resource-level or narrower scope.'
    }
    $null = [guid]::Parse($binding.principalId)
    if (-not $binding.role) { throw 'An explicit approved role is required.' }
    $existing = az role assignment list --assignee-object-id $binding.principalId `
        --scope $binding.scope --query "[?roleDefinitionName=='$($binding.role)'].id" -o tsv
    if ($LASTEXITCODE -ne 0) { throw 'Role lookup failed.' }
    if ($existing) { Write-Output 'role_present'; continue }
    if (-not $Apply) { Write-Output 'role_missing'; continue }
    $arguments = @(
        'role', 'assignment', 'create', '--assignee-object-id', $binding.principalId,
        '--assignee-principal-type', 'ServicePrincipal', '--role', $binding.role,
        '--scope', $binding.scope, '--output', 'none'
    )
    if ($binding.condition) {
        $arguments += @('--condition', $binding.condition, '--condition-version', '2.0')
    }
    & az @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Role assignment failed.' }
    Write-Output 'role_assigned'
}
# App roles are Entra permissions, not Azure RBAC.
foreach ($binding in $data.appRoles) {
    foreach ($value in @($binding.principalId, $binding.resourcePrincipalId, $binding.appRoleId)) {
        $null = [guid]::Parse($value)
    }
    if ($data.graphEndpoint -notin @('https://graph.microsoft.us', 'https://dod-graph.microsoft.us')) {
        throw 'Select the approved Government Graph endpoint explicitly.'
    }
    $url = "$($data.graphEndpoint)/v1.0/servicePrincipals/$($binding.principalId)/appRoleAssignments"
    $assignments = @()
    $pageUrl = $url
    do {
        $page = az rest --method GET --url $pageUrl -o json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Application-role lookup failed.' }
        $assignments += $page.value
        $pageUrl = $page.'@odata.nextLink'
        if ($pageUrl -and -not $pageUrl.StartsWith("$($data.graphEndpoint)/v1.0/")) {
            throw 'Unexpected application-role pagination endpoint.'
        }
    } while ($pageUrl)
    $present = $assignments | Where-Object {
        $_.resourceId -eq $binding.resourcePrincipalId -and $_.appRoleId -eq $binding.appRoleId
    }
    if ($present) { Write-Output 'app_role_present'; continue }
    if (-not $Apply) { Write-Output 'app_role_missing'; continue }
    $body = @{
        principalId = $binding.principalId
        resourceId = $binding.resourcePrincipalId
        appRoleId = $binding.appRoleId
    } | ConvertTo-Json -Compress
    # File input avoids Windows native-command JSON quoting ambiguities.
    $bodyFile = New-TemporaryFile
    try {
        [System.IO.File]::WriteAllText($bodyFile.FullName, $body)
        az rest --method POST --url $url --body "@$($bodyFile.FullName)" --output none
        if ($LASTEXITCODE -ne 0) { throw 'Application-role assignment failed.' }
    } finally {
        Remove-Item -LiteralPath $bodyFile.FullName
    }
    Write-Output 'app_role_assigned'
}
