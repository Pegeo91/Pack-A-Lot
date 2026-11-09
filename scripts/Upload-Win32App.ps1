<#
.SYNOPSIS
    Uploads a .intunewin package to Intune as a Win32 app using the Microsoft Graph PowerShell SDK
    (Connect-MgGraph / Invoke-MgGraphRequest).

.PARAMETER ConfigPath
    JSON file written by uploader.py:
      { intunewin, appJsonPath, assignmentsJsonPath, auth = { mode, tenantId, clientId, certThumbprint } }
    A client secret (auth mode 'secret') is read from $env:PACKALOT_CLIENT_SECRET, never from disk.
#>
param([Parameter(Mandatory)][string]$ConfigPath)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Log([string]$Message) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) }

$cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$base = 'https://graph.microsoft.com/beta/deviceAppManagement/mobileApps'
$appId = $null
$committed = $false
$tmp = Join-Path $env:TEMP ('packalot_' + [guid]::NewGuid().ToString('N'))

function Invoke-Graph([string]$Method, [string]$Uri, $Body) {
    $p = @{ Method = $Method; Uri = $Uri; OutputType = 'PSObject' }
    if ($null -ne $Body) {
        $p.Body = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 10 -Compress }
        $p.ContentType = 'application/json'
    }
    Invoke-MgGraphRequest @p
}

function Wait-FileState([string]$Uri, [string]$Success, [string]$Failure, [int]$TimeoutSeconds = 600) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $f = Invoke-Graph GET $Uri
        if ($f.uploadState -eq $Success) { return $f }
        if ($f.uploadState -eq $Failure) { throw "File upload state: $($f.uploadState)" }
        Start-Sleep -Seconds 3
    }
    throw "Timed out waiting for upload state '$Success'"
}

function Invoke-BlobPut([string]$Uri, $Body, [string]$ContentType, [hashtable]$Headers = @{}) {
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Invoke-WebRequest -Method Put -Uri $Uri -Body $Body -ContentType $ContentType -Headers $Headers -UseBasicParsing | Out-Null
            return
        }
        catch {
            if ($attempt -eq 4) { throw }
            Write-Log "Blob upload attempt $attempt failed ($($_.Exception.Message)), retrying..."
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
}

try {
    # ------------------------------------------------------------------ 1. Graph module
    . (Join-Path $PSScriptRoot 'PackALot.Graph.ps1')
    $logFn = { param($m) Write-Log $m }
    Install-PackALotGraphModule -Log $logFn

    # ------------------------------------------------------------------ 2. Connect
    $auth = $cfg.auth
    # Assignments may name groups by display name instead of object ID; those are resolved after sign-in
    $assignObj = $null
    $guidRx = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
    if ($cfg.assignmentsJsonPath -and (Test-Path -LiteralPath $cfg.assignmentsJsonPath)) {
        $assignObj = Get-Content -LiteralPath $cfg.assignmentsJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $byName = @($assignObj.mobileAppAssignments | Where-Object { $_.target.groupId -and $_.target.groupId -notmatch $guidRx })
    $scopes = @('DeviceManagementApps.ReadWrite.All')
    if ($byName.Count) { $scopes += 'Group.Read.All' }
    $ctx = Connect-PackALotGraph -Auth $auth -Scopes $scopes -Log $logFn

    # ------------------------------------------------------------------ 2b. Resolve group names
    # Done before anything is created, so a mistyped group name fails without leaving a half-made app
    foreach ($a in $byName) {
        $name = $a.target.groupId
        $filter = [uri]::EscapeDataString("displayName eq '$($name.Replace("'", "''"))'")
        try {
            $found = @((Invoke-Graph GET "https://graph.microsoft.com/v1.0/groups?`$filter=$filter&`$select=id,displayName").value)
        }
        catch {
            throw "Could not look up group '$name' ($($_.Exception.Message)). Looking up groups by name needs the Group.Read.All permission - grant it, or enter the group's object ID instead."
        }
        if ($found.Count -eq 0) { throw "Group '$name' was not found in Entra ID (the name must match exactly)." }
        if ($found.Count -gt 1) {
            throw "$($found.Count) groups are named '$name' ($(($found | ForEach-Object { $_.id }) -join ', ')). Enter the object ID of the one you mean."
        }
        Write-Log "Group '$name' -> $($found[0].id)"
        $a.target.groupId = $found[0].id
    }

    # ------------------------------------------------------------------ 3. Read the .intunewin
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($cfg.intunewin)
    try {
        $detEntry = $zip.Entries | Where-Object { $_.FullName -match 'Metadata/Detection\.xml$' } | Select-Object -First 1
        if (-not $detEntry) { throw 'Detection.xml not found in .intunewin' }
        $reader = New-Object IO.StreamReader($detEntry.Open())
        [xml]$detXml = $reader.ReadToEnd()
        $reader.Close()
        $info = $detXml.ApplicationInfo
        $encName = $info.FileName
        $encEntry = $zip.Entries | Where-Object { $_.FullName -match ('Contents/' + [regex]::Escape($encName) + '$') } | Select-Object -First 1
        if (-not $encEntry) { throw "Encrypted content $encName not found in .intunewin" }
        $encFile = Join-Path $tmp $encName
        [IO.Compression.ZipFileExtensions]::ExtractToFile($encEntry, $encFile, $true)
    }
    finally { $zip.Dispose() }
    $encSize = (Get-Item -LiteralPath $encFile).Length
    Write-Log ("Package: {0}  ({1:N1} MB unencrypted, {2:N1} MB encrypted)" -f $info.Name, ([int64]$info.UnencryptedContentSize / 1MB), ($encSize / 1MB))

    # ------------------------------------------------------------------ 4. Create the app
    $appJson = Get-Content -LiteralPath $cfg.appJsonPath -Raw -Encoding UTF8
    Write-Log 'Creating Win32 app...'
    $app = Invoke-Graph POST $base $appJson
    $appId = $app.id
    Write-Log "App created: $appId"

    # ------------------------------------------------------------------ 5. Content version + file
    $cvBase = "$base/$appId/microsoft.graph.win32LobApp/contentVersions"
    $cv = Invoke-Graph POST $cvBase '{}'
    Write-Log "Content version: $($cv.id)"
    $fileBody = @{
        '@odata.type' = '#microsoft.graph.mobileAppContentFile'
        name          = $encName
        size          = [int64]$info.UnencryptedContentSize
        sizeEncrypted = [int64]$encSize
        manifest      = $null
        isDependency  = $false
    }
    $file = Invoke-Graph POST "$cvBase/$($cv.id)/files" $fileBody
    $fileUri = "$cvBase/$($cv.id)/files/$($file.id)"
    $file = Wait-FileState $fileUri 'azureStorageUriRequestSuccess' 'azureStorageUriRequestFailed'
    $sas = $file.azureStorageUri

    # ------------------------------------------------------------------ 6. Chunked blob upload
    $chunkSize = 6MB
    $blockIds = New-Object System.Collections.Generic.List[string]
    $stream = [IO.File]::OpenRead($encFile)
    $buffer = New-Object byte[] $chunkSize
    $sasTimer = [Diagnostics.Stopwatch]::StartNew()
    $index = 0
    $uploaded = 0
    try {
        while (($read = $stream.Read($buffer, 0, $chunkSize)) -gt 0) {
            if ($sasTimer.Elapsed.TotalMinutes -gt 7) {
                Write-Log 'Renewing upload URI...'
                Invoke-Graph POST "$fileUri/renewUpload" '{}' | Out-Null
                $file = Wait-FileState $fileUri 'azureStorageUriRenewalSuccess' 'azureStorageUriRenewalFailed'
                $sas = $file.azureStorageUri
                $sasTimer.Restart()
            }
            $chunk = New-Object byte[] $read
            [Array]::Copy($buffer, $chunk, $read)
            $blockId = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($index.ToString('0000000')))
            Invoke-BlobPut "$sas&comp=block&blockid=$([uri]::EscapeDataString($blockId))" $chunk 'application/octet-stream' @{ 'x-ms-blob-type' = 'BlockBlob' }
            $blockIds.Add($blockId)
            $uploaded += $read
            $index++
            Write-Log ("Uploaded {0:N1} / {1:N1} MB ({2:N0}%)" -f ($uploaded / 1MB), ($encSize / 1MB), (100 * $uploaded / [math]::Max($encSize, 1)))
        }
    }
    finally { $stream.Close() }
    $blockList = '<?xml version="1.0" encoding="utf-8"?><BlockList>' + (($blockIds | ForEach-Object { "<Latest>$_</Latest>" }) -join '') + '</BlockList>'
    Invoke-BlobPut "$sas&comp=blocklist" $blockList 'application/xml'

    # ------------------------------------------------------------------ 7. Commit
    $enc = $info.EncryptionInfo
    $commitBody = @{
        fileEncryptionInfo = @{
            encryptionKey        = $enc.EncryptionKey
            macKey               = $enc.MacKey
            initializationVector = $enc.InitializationVector
            mac                  = $enc.Mac
            profileIdentifier    = 'ProfileVersion1'
            fileDigest           = $enc.FileDigest
            fileDigestAlgorithm  = $enc.FileDigestAlgorithm
        }
    }
    Write-Log 'Committing file...'
    Invoke-Graph POST "$fileUri/commit" $commitBody | Out-Null
    Wait-FileState $fileUri 'commitFileSuccess' 'commitFileFailed' | Out-Null
    Invoke-Graph PATCH "$base/$appId" @{ '@odata.type' = '#microsoft.graph.win32LobApp'; committedContentVersion = "$($cv.id)" } | Out-Null
    $committed = $true
    Write-Log 'Content committed.'

    # ------------------------------------------------------------------ 8. Assignments
    if ($assignObj) {
        Write-Log 'Creating assignments...'
        Invoke-Graph POST "$base/$appId/assign" ($assignObj | ConvertTo-Json -Depth 10 -Compress) | Out-Null
        Write-Log 'Assignments created.'
    }

    Write-Log 'Upload completed successfully.'
    Write-Host ('RESULT:' + (@{ appId = $appId; tenantId = $ctx.TenantId } | ConvertTo-Json -Compress))
    exit 0
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    if ($_.ErrorDetails.Message) { Write-Log $_.ErrorDetails.Message }
    if ($appId -and -not $committed -and $cfg.cleanupOnFailure) {
        try { Invoke-Graph DELETE "$base/$appId" | Out-Null; Write-Log "Removed incomplete app $appId" } catch { Write-Log "Could not remove incomplete app $appId" }
    }
    exit 1
}
finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
