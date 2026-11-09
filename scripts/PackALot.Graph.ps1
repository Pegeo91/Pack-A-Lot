# Shared Microsoft Graph helpers, dot-sourced by Upload-Win32App.ps1 and Test-IntuneConnection.ps1.

function Install-PackALotGraphModule {
    <# Makes sure Microsoft.Graph.Authentication is installed (CurrentUser, no admin needed) and imported. #>
    param([scriptblock]$Log = { param($m) Write-Host $m })
    if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
        & $Log 'Microsoft.Graph.Authentication not found - installing for CurrentUser...'
        if (-not (Get-PackageProvider -ListAvailable -Name NuGet -ErrorAction SilentlyContinue)) {
            Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force | Out-Null
        }
        Install-Module -Name Microsoft.Graph.Authentication -Scope CurrentUser -Repository PSGallery -Force -AllowClobber
    }
    Import-Module Microsoft.Graph.Authentication
}

function Connect-PackALotGraph {
    <#
    .SYNOPSIS
        Connect-MgGraph with the auth settings from config.yaml ({ mode, tenantId, clientId, certThumbprint }).
        A client secret is read from $env:PACKALOT_CLIENT_SECRET, never from disk.
    #>
    param(
        [Parameter(Mandatory)]$Auth,
        [string[]]$Scopes = @('DeviceManagementApps.ReadWrite.All'),
        [scriptblock]$Log = { param($m) Write-Host $m }
    )
    $connect = @{ NoWelcome = $true }
    if ($Auth.tenantId) { $connect.TenantId = $Auth.tenantId }
    switch ($Auth.mode) {
        'secret' {
            if (-not $env:PACKALOT_CLIENT_SECRET) { throw 'Client secret not provided.' }
            $sec = ConvertTo-SecureString $env:PACKALOT_CLIENT_SECRET -AsPlainText -Force
            $connect.ClientSecretCredential = New-Object System.Management.Automation.PSCredential($Auth.clientId, $sec)
        }
        'certificate' {
            $connect.ClientId = $Auth.clientId
            $connect.CertificateThumbprint = $Auth.certThumbprint
        }
        'devicecode' {
            $connect.Scopes = $Scopes
            $connect.UseDeviceCode = $true
            if ($Auth.clientId) { $connect.ClientId = $Auth.clientId }
        }
        default {
            $connect.Scopes = $Scopes
            if ($Auth.clientId) { $connect.ClientId = $Auth.clientId }
        }
    }
    & $Log "Connecting to Microsoft Graph ($($Auth.mode))..."
    if ($Auth.mode -eq 'interactive') {
        # Interactive sign-in uses the Windows broker (WAM), which must attach its dialog to a window.
        # The Python side starts these scripts with their own console window for that reason
        # (Set-MgGraphOption -DisableLoginByWAM is ignored by Microsoft.Graph.Authentication 2.41+).
        try { $Host.UI.RawUI.WindowTitle = 'Pack-A-Lot - Microsoft sign-in (do not close)' } catch { }
        & $Log 'A Windows sign-in window will appear - complete the sign-in there.'
        Connect-MgGraph @connect
        # Signed in: minimise the helper console for the rest of the run
        try {
            if (-not ('PackALot.Win' -as [type])) {
                Add-Type -Namespace PackALot -Name Win -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
            }
            [void][PackALot.Win]::ShowWindow([PackALot.Win]::GetConsoleWindow(), 6)  # SW_MINIMIZE
        } catch { }
    }
    else {
        Connect-MgGraph @connect
    }
    $ctx = Get-MgContext
    & $Log "Connected. Tenant: $($ctx.TenantId)  Account/App: $(if ($ctx.Account) { $ctx.Account } else { $ctx.AppName })"
    $ctx
}
