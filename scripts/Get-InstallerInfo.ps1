<#
.SYNOPSIS
    Reads metadata from an MSI (Property table) or EXE (version resource). Outputs JSON.
#>
param([Parameter(Mandatory)][string]$Path)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$result = [ordered]@{}

if ([IO.Path]::GetExtension($Path) -eq '.msi') {
    $installer = New-Object -ComObject WindowsInstaller.Installer
    $db = $installer.GetType().InvokeMember('OpenDatabase', 'InvokeMethod', $null, $installer, @($Path, 0))
    function Get-MsiProperty([string]$Name) {
        $view = $db.GetType().InvokeMember('OpenView', 'InvokeMethod', $null, $db, @("SELECT Value FROM Property WHERE Property='$Name'"))
        $view.GetType().InvokeMember('Execute', 'InvokeMethod', $null, $view, $null) | Out-Null
        $record = $view.GetType().InvokeMember('Fetch', 'InvokeMethod', $null, $view, $null)
        $value = if ($record) { $record.GetType().InvokeMember('StringData', 'GetProperty', $null, $record, 1) } else { '' }
        $view.GetType().InvokeMember('Close', 'InvokeMethod', $null, $view, $null) | Out-Null
        return $value
    }
    foreach ($p in 'ProductCode', 'ProductName', 'ProductVersion', 'Manufacturer', 'UpgradeCode', 'ALLUSERS') {
        $result[$p] = Get-MsiProperty $p
    }
    [Runtime.InteropServices.Marshal]::ReleaseComObject($db) | Out-Null
    [Runtime.InteropServices.Marshal]::ReleaseComObject($installer) | Out-Null
}
else {
    $vi = (Get-Item -LiteralPath $Path).VersionInfo
    $result.ProductName = "$($vi.ProductName)".Trim()
    $result.ProductVersion = "$($vi.ProductVersion)".Trim()
    $result.FileVersion = "$($vi.FileVersion)".Trim()
    $result.Manufacturer = "$($vi.CompanyName)".Trim()
    $result.FileDescription = "$($vi.FileDescription)".Trim()
}

$result | ConvertTo-Json -Compress
