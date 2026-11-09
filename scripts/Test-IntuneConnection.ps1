<#
.SYNOPSIS
    Signs in to Microsoft Graph with the Pack-A-Lot settings and checks the permissions the upload needs.

.PARAMETER ConfigPath
    JSON file: { auth = { mode, tenantId, clientId, certThumbprint } }. A client secret comes from
    $env:PACKALOT_CLIENT_SECRET. Prints progress, then one line 'RESULT:{json}'.
#>
param([Parameter(Mandatory)][string]$ConfigPath)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Log([string]$Message) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) }
$result = [ordered]@{ connected = $false; tenantId = ''; account = ''; scopes = @(); apps = $false; groups = $false; error = '' }

try {
    $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    . (Join-Path $PSScriptRoot 'PackALot.Graph.ps1')
    $logFn = { param($m) Write-Log $m }
    Install-PackALotGraphModule -Log $logFn
    $ver = (Get-Module Microsoft.Graph.Authentication).Version
    Write-Log "Microsoft.Graph.Authentication $ver"

    $ctx = Connect-PackALotGraph -Auth $cfg.auth -Scopes @('DeviceManagementApps.ReadWrite.All') -Log $logFn
    $result.connected = $true
    $result.tenantId = "$($ctx.TenantId)"
    $result.account = if ($ctx.Account) { "$($ctx.Account)" } else { "$($ctx.AppName)" }
    $result.scopes = @($ctx.Scopes)
    Write-Log "Permissions in the token: $(@($ctx.Scopes) -join ', ')"

    try {
        Invoke-MgGraphRequest -Method GET -Uri 'https://graph.microsoft.com/beta/deviceAppManagement/mobileApps?$top=1&$select=id' -OutputType PSObject | Out-Null
        $result.apps = $true
        Write-Log 'PASS  Intune apps can be read (DeviceManagementApps.ReadWrite.All).'
    }
    catch {
        Write-Log "FAIL  Intune apps cannot be read: $($_.Exception.Message)"
    }
    try {
        Invoke-MgGraphRequest -Method GET -Uri 'https://graph.microsoft.com/v1.0/groups?$top=1&$select=id' -OutputType PSObject | Out-Null
        $result.groups = $true
        Write-Log 'PASS  Groups can be read (assignments by group name will work).'
    }
    catch {
        Write-Log 'INFO  Groups cannot be read - enter group object IDs in assignments, or grant Group.Read.All.'
    }
}
catch {
    $result.error = $_.Exception.Message
    Write-Log "ERROR: $($_.Exception.Message)"
}
Write-Host ('RESULT:' + ($result | ConvertTo-Json -Compress))
if (-not ($result.connected -and $result.apps)) { exit 1 }
