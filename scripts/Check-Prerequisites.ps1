<#
.SYNOPSIS
    Checks (and optionally fixes) everything Pack-A-Lot needs on this PC.

.DESCRIPTION
    Checks: Windows edition, CPU virtualization, the Windows Sandbox feature, winget, the PowerShell
    execution policy, Microsoft.Graph.Authentication, IntuneWinAppUtil.exe, the PSADT template, the Python
    runtime, config.yaml (incl. the certificate for certificate auth) and network access to the services used.

.PARAMETER Json
    Print the results as JSON (used by the web UI).
.PARAMETER Fix
    IDs of checks to fix (sandbox, winget, graph_module, intunewin). Fixes that need admin rights
    re-launch this script elevated (UAC prompt).
.PARAMETER Interactive
    Console mode for Setup.cmd: show the results and offer to fix what can be fixed.
#>
param(
    [switch]$Json,
    [string[]]$Fix = @(),
    [switch]$Interactive,
    [string]$Root = (Split-Path $PSScriptRoot -Parent)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$SandboxExe = Join-Path $env:windir 'System32\WindowsSandbox.exe'
$IntuneWinUrl = 'https://github.com/microsoft/Microsoft-Win32-Content-Prep-Tool/raw/master/IntuneWinAppUtil.exe'

# ------------------------------------------------------------------------- helpers
function New-Check([string]$Id, [string]$Name, [string]$Status, [string]$Detail, [string]$FixLabel = '',
                   [bool]$Admin = $false, [bool]$Reboot = $false, [string]$Area = '') {
    [pscustomobject][ordered]@{ id = $Id; name = $Name; status = $Status; detail = $Detail; fix = $FixLabel
                                 admin = $Admin; reboot = $Reboot; area = $Area }
}

function Get-Settings {
    $s = @{ intunewin_util = (Join-Path $Root 'tools\IntuneWinAppUtil.exe'); template_dir = (Join-Path $Root 'PSADT Template') }
    $f = Join-Path $Root 'settings.json'
    if (Test-Path $f) {
        try {
            $j = Get-Content $f -Raw | ConvertFrom-Json
            foreach ($k in 'intunewin_util', 'template_dir') { if ($j.$k -and (Test-Path $j.$k)) { $s[$k] = $j.$k } }
        } catch { }
    }
    $s
}

function Get-AuthConfig {
    $c = @{ auth_mode = 'interactive'; tenant_id = ''; client_id = ''; cert_thumbprint = '' }
    $f = Join-Path $Root 'config.yaml'
    if (Test-Path $f) {
        foreach ($line in Get-Content $f) {
            if ($line -match '^\s*(auth_mode|tenant_id|client_id|cert_thumbprint)\s*:\s*(.*?)\s*(#.*)?$') {
                $c[$Matches[1]] = $Matches[2].Trim('"', "'", ' ')
            }
        }
    }
    $c
}

function Test-Host([string]$Url) {
    # Any HTTP answer (even 403/404) means the host is reachable through firewall / proxy
    try {
        $req = [Net.HttpWebRequest]::Create($Url)
        $req.Method = 'HEAD'; $req.Timeout = 8000; $req.AllowAutoRedirect = $false
        $req.UserAgent = 'Pack-A-Lot'
        $req.GetResponse().Close(); return $true
    }
    catch [Net.WebException] { return [bool]$_.Exception.Response }
    catch { return $false }
}

# ------------------------------------------------------------------------- checks
function Get-Checks {
    $checks = @()
    $os = Get-CimInstance Win32_OperatingSystem
    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $edition = "$($cv.EditionID)"
    $build = [int]$cv.CurrentBuildNumber

    # Windows edition / build (Windows Sandbox: Pro, Enterprise, Education; build 18305+)
    $okEdition = $edition -notmatch '^(Core|Home)'
    $checks += New-Check 'windows' 'Windows edition' $(if ($okEdition -and $build -ge 18305) { 'ok' } else { 'fail' }) `
        $(if (-not $okEdition) { "$($os.Caption) - Windows Sandbox needs Pro, Enterprise or Education. Packaging and upload still work; sandbox testing does not." }
          elseif ($build -lt 18305) { "Build $build is too old for Windows Sandbox (needs 18305 / 1903 or later)." }
          else { "$($os.Caption) (build $build)" }) -Area 'Sandbox'

    # CPU virtualization
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $isVm = "$($cs.Model) $($cs.Manufacturer)" -match 'Virtual|VMware|KVM|QEMU|Xen|Parallels'
    if ($cs.HypervisorPresent -or $cpu.VirtualizationFirmwareEnabled) {
        $checks += New-Check 'virtualization' 'CPU virtualization' 'ok' $(if ($cs.HypervisorPresent) { 'Hypervisor is running.' } else { 'Enabled in firmware.' }) -Area 'Sandbox'
    }
    else {
        $msg = if ($isVm) { 'This is a virtual machine - enable nested virtualization on the host (Hyper-V: Set-VMProcessor -ExposeVirtualizationExtensions $true).' }
               else { 'Virtualization is off - enable Intel VT-x / AMD-V (SVM) in the BIOS/UEFI settings.' }
        $checks += New-Check 'virtualization' 'CPU virtualization' 'fail' $msg -Area 'Sandbox'
    }

    # Windows Sandbox optional feature
    $pending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
    $state = $null
    if ($IsAdmin) { try { $state = (Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM).State } catch { } }
    if (Test-Path $SandboxExe) {
        $checks += New-Check 'sandbox' 'Windows Sandbox' 'ok' 'Feature is enabled.' -Area 'Sandbox'
    }
    elseif ("$state" -eq 'EnablePending' -or $pending) {
        $checks += New-Check 'sandbox' 'Windows Sandbox' 'warn' 'Feature is being enabled - restart the PC to finish.' -Reboot $true -Area 'Sandbox'
    }
    elseif ($okEdition) {
        $checks += New-Check 'sandbox' 'Windows Sandbox' 'fail' 'The Windows Sandbox feature is not enabled.' 'Enable Windows Sandbox' -Admin $true -Reboot $true -Area 'Sandbox'
    }
    else {
        $checks += New-Check 'sandbox' 'Windows Sandbox' 'fail' 'Not available on this Windows edition.' -Area 'Sandbox'
    }

    # winget
    $wg = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($wg) {
        $v = try { (& winget.exe --version 2>$null | Select-Object -First 1) } catch { '' }
        $checks += New-Check 'winget' 'winget' 'ok' "winget $v" -Area 'Packaging'
    }
    else {
        $checks += New-Check 'winget' 'winget' 'fail' 'winget (App Installer) is missing - needed for winget sources. Own installers still work.' 'Install winget' -Area 'Packaging'
    }

    # IntuneWinAppUtil + PSADT template + runtime
    $s = Get-Settings
    $checks += if (Test-Path $s.intunewin_util) { New-Check 'intunewin' 'IntuneWinAppUtil.exe' 'ok' $s.intunewin_util -Area 'Packaging' }
               else { New-Check 'intunewin' 'IntuneWinAppUtil.exe' 'fail' 'Missing - needed to create .intunewin files.' 'Download' -Area 'Packaging' }
    $tpl = Join-Path $s.template_dir 'Invoke-AppDeployToolkit.ps1'
    $checks += if (Test-Path $tpl) { New-Check 'template' 'PSADT template' 'ok' $s.template_dir -Area 'Packaging' }
               else { New-Check 'template' 'PSADT template' 'fail' "Not found in $($s.template_dir) - copy the 'PSADT Template' folder with Pack-A-Lot." -Area 'Packaging' }
    $py = Join-Path $Root 'runtime\python\python.exe'
    $exe = Join-Path $Root 'Pack-A-Lot.exe'
    $runtimes = @()
    if (Test-Path $exe) { $runtimes += 'Pack-A-Lot.exe (Python built in)' }
    if (Test-Path $py) { $runtimes += 'runtime\python (portable Python for start.cmd)' }
    $checks += if ($runtimes) { New-Check 'python' 'Python runtime' 'ok' ($runtimes -join '; ') -Area 'Packaging' }
               else { New-Check 'python' 'Python runtime' 'fail' 'Neither Pack-A-Lot.exe nor runtime\python is present - run Prepare-Portable.ps1 on a PC with internet access.' -Area 'Packaging' }

    # PowerShell: execution policy set by Group Policy overrides -ExecutionPolicy Bypass
    $gpo = @(Get-ExecutionPolicy -List | Where-Object { $_.Scope -in 'MachinePolicy', 'UserPolicy' -and "$($_.ExecutionPolicy)" -in 'Restricted', 'AllSigned' })
    $lang = $ExecutionContext.SessionState.LanguageMode
    $checks += if ($gpo) { New-Check 'policy' 'PowerShell script policy' 'fail' "Group Policy sets '$($gpo[0].ExecutionPolicy)' ($($gpo[0].Scope)) - Pack-A-Lot's unsigned scripts are blocked. Ask your admin for an exception." -Area 'PowerShell' }
               elseif ("$lang" -ne 'FullLanguage') { New-Check 'policy' 'PowerShell script policy' 'warn' "PowerShell runs in $lang (AppLocker/WDAC) - some steps may fail." -Area 'PowerShell' }
               else { New-Check 'policy' 'PowerShell script policy' 'ok' "Scripts allowed ($(Get-ExecutionPolicy), PowerShell $($PSVersionTable.PSVersion))" -Area 'PowerShell' }

    # Graph module
    $mod = Get-Module -ListAvailable Microsoft.Graph.Authentication | Sort-Object Version -Descending | Select-Object -First 1
    $checks += if ($mod) { New-Check 'graph_module' 'Microsoft.Graph.Authentication' 'ok' "Version $($mod.Version)" -Area 'Intune' }
               else { New-Check 'graph_module' 'Microsoft.Graph.Authentication' 'warn' 'Not installed (installed automatically on the first upload, or now).' 'Install module' -Area 'Intune' }

    # config.yaml
    $c = Get-AuthConfig
    $mode = $c.auth_mode
    $guid = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
    $cfgCheck = $null
    if ($mode -in 'secret', 'certificate') {
        if (-not ($c.tenant_id -and $c.client_id)) {
            $cfgCheck = New-Check 'config' 'Intune app registration' 'fail' "auth_mode '$mode' needs tenant_id and client_id in config.yaml (or set them on the Upload step)." -Area 'Intune'
        }
        elseif ($mode -eq 'certificate') {
            $t = ($c.cert_thumbprint -replace '\s', '').ToUpper()
            $cert = if ($t) { Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue | Where-Object Thumbprint -eq $t | Select-Object -First 1 }
            $cfgCheck = if (-not $t) { New-Check 'config' 'Intune app registration' 'fail' 'cert_thumbprint is empty in config.yaml.' -Area 'Intune' }
                        elseif (-not $cert) { New-Check 'config' 'Intune app registration' 'fail' "Certificate $t is not installed on this PC - import the .pfx (with private key) into Cert:\CurrentUser\My." -Area 'Intune' }
                        elseif (-not $cert.HasPrivateKey) { New-Check 'config' 'Intune app registration' 'fail' "Certificate $t has no private key on this PC - import the .pfx, not the .cer." -Area 'Intune' }
                        elseif ($cert.NotAfter -lt (Get-Date)) { New-Check 'config' 'Intune app registration' 'fail' "Certificate $t expired on $($cert.NotAfter.ToString('yyyy-MM-dd'))." -Area 'Intune' }
                        else { New-Check 'config' 'Intune app registration' 'ok' "Certificate auth, cert valid until $($cert.NotAfter.ToString('yyyy-MM-dd'))." -Area 'Intune' }
        }
        else {
            $cfgCheck = New-Check 'config' 'Intune app registration' 'ok' 'Client secret auth (the secret is entered at upload time, never stored).' -Area 'Intune'
        }
    }
    elseif (($c.tenant_id -and $c.tenant_id -notmatch $guid -and $c.tenant_id -notmatch '\.') -or ($c.client_id -and $c.client_id -notmatch $guid)) {
        $cfgCheck = New-Check 'config' 'Intune app registration' 'warn' 'tenant_id / client_id in config.yaml do not look like valid IDs.' -Area 'Intune'
    }
    else {
        $cfgCheck = New-Check 'config' 'Intune app registration' 'ok' $(if ($c.client_id) { "$mode sign-in with your app registration." } else { "$mode sign-in with the Microsoft Graph Command Line Tools app." }) -Area 'Intune'
    }
    $checks += $cfgCheck

    # Network
    $hosts = [ordered]@{
        'cdn.winget.microsoft.com'   = 'winget source'
        'github.com'                 = 'most winget installers, IntuneWinAppUtil'
        'www.powershellgallery.com'  = 'Graph module install'
        'login.microsoftonline.com'  = 'Intune sign-in'
        'graph.microsoft.com'        = 'Intune upload'
    }
    $down = @($hosts.Keys | Where-Object { -not (Test-Host "https://$_/") })
    $checks += if (-not $down) { New-Check 'network' 'Network access' 'ok' "Reachable: $($hosts.Keys -join ', ')" -Area 'Network' }
               else { New-Check 'network' 'Network access' $(if ($down -contains 'graph.microsoft.com' -or $down -contains 'login.microsoftonline.com') { 'fail' } else { 'warn' }) `
                      ("Not reachable: " + (($down | ForEach-Object { "$_ ($($hosts[$_]))" }) -join '; ') + '. Check firewall / proxy rules.') -Area 'Network' }
    $checks
}

# ------------------------------------------------------------------------- fixes
function Invoke-Fix([string]$Id) {
    switch ($Id) {
        'sandbox' {
            if (-not $IsAdmin) {
                Write-Host 'Enabling Windows Sandbox needs administrator rights - confirm the UAC prompt...'
                $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList @(
                    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Fix', 'sandbox', '-Root', "`"$Root`"")
                if ($p.ExitCode -ne 0) { throw "Enabling Windows Sandbox failed (exit code $($p.ExitCode))." }
                return 'Windows Sandbox enabled - restart the PC to finish.'
            }
            $r = Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All -NoRestart
            return $(if ($r.RestartNeeded) { 'Windows Sandbox enabled - restart the PC to finish.' } else { 'Windows Sandbox enabled.' })
        }
        'winget' {
            try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe } catch { }
            Start-Sleep -Seconds 2
            if (Get-Command winget.exe -ErrorAction SilentlyContinue) { return 'winget is available.' }
            Start-Process 'ms-windows-store://pdp/?ProductId=9NBLGGH4NNS1'
            throw 'winget could not be registered automatically - the Microsoft Store page for App Installer was opened. Install it there.'
        }
        'graph_module' {
            . (Join-Path $PSScriptRoot 'PackALot.Graph.ps1')
            Install-PackALotGraphModule -Log { param($m) Write-Host $m }
            return "Microsoft.Graph.Authentication $((Get-Module Microsoft.Graph.Authentication).Version) installed."
        }
        'intunewin' {
            $dest = Join-Path $Root 'tools\IntuneWinAppUtil.exe'
            New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
            Invoke-WebRequest -Uri $IntuneWinUrl -OutFile "$dest.download" -UseBasicParsing -TimeoutSec 300
            Move-Item "$dest.download" $dest -Force
            return "Downloaded to $dest."
        }
        default { throw "No automatic fix for '$Id'." }
    }
}

# ------------------------------------------------------------------------- main
if ($Fix) {
    $out = foreach ($id in $Fix) {
        try { [pscustomobject]@{ id = $id; ok = $true; message = (Invoke-Fix $id) } }
        catch { [pscustomobject]@{ id = $id; ok = $false; message = $_.Exception.Message } }
    }
    if ($Json) { ConvertTo-Json @($out) -Compress } else { $out | ForEach-Object { Write-Host "$($_.id): $($_.message)" } }
    if (@($out | Where-Object { -not $_.ok })) { exit 1 }
    exit 0
}

$checks = @(Get-Checks)
if ($Json) {
    ConvertTo-Json @{ admin = $IsAdmin; checks = $checks } -Depth 4 -Compress
    exit 0
}

# Console report (Setup.cmd)
$colors = @{ ok = 'Green'; warn = 'Yellow'; fail = 'Red' }
$marks = @{ ok = ' OK '; warn = 'WARN'; fail = 'FAIL' }
Write-Host ''
Write-Host 'Pack-A-Lot - system check' -ForegroundColor Cyan
Write-Host ('-' * 60)
foreach ($c in $checks) {
    Write-Host ('[{0}] ' -f $marks[$c.status]) -ForegroundColor $colors[$c.status] -NoNewline
    Write-Host ('{0,-32}' -f $c.name) -NoNewline
    Write-Host $c.detail -ForegroundColor DarkGray
}
Write-Host ''
$fixable = @($checks | Where-Object { $_.status -ne 'ok' -and $_.fix })
if ($Interactive -and $fixable) {
    foreach ($c in $fixable) {
        $note = @(); if ($c.admin) { $note += 'admin' }; if ($c.reboot) { $note += 'restart needed' }
        $answer = Read-Host ("{0}? {1}[Y/n]" -f $c.fix, $(if ($note) { "($($note -join ', ')) " } else { '' }))
        if ($answer -match '^(|y|yes)$') {
            try { Write-Host ('  ' + (Invoke-Fix $c.id)) -ForegroundColor Green }
            catch { Write-Host ('  ' + $_.Exception.Message) -ForegroundColor Red }
        }
    }
    Write-Host ''
}
$problems = @($checks | Where-Object { $_.status -eq 'fail' })
if ($problems) { Write-Host "$($problems.Count) problem(s) remaining - see above." -ForegroundColor Yellow }
else { Write-Host 'Everything Pack-A-Lot needs is in place.' -ForegroundColor Green }
