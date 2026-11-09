<#
.SYNOPSIS
    Downloads everything Pack-A-Lot needs to run from this folder on another PC, without installing Python.

.DESCRIPTION
    - runtime\python\         Python (official embeddable package) + Flask + PyYAML
    - tools\IntuneWinAppUtil.exe

    Run it once on a PC with internet access, then copy the whole Pack-A-Lot folder to the target PC.
    There, run Setup.cmd to check / fix Windows Sandbox, winget, the Graph module and the Intune connection.

.PARAMETER PythonVersion
    Python version to bundle (must exist as an embeddable package on python.org).

.PARAMETER Force
    Re-download even if the files are already there.
#>
param(
    [string]$PythonVersion = '3.14.5',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = $PSScriptRoot
$runtime = Join-Path $root 'runtime'
$pyDir = Join-Path $runtime 'python'
$tools = Join-Path $root 'tools'
$tmp = Join-Path $env:TEMP ('packalot-prep-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp, $runtime, $tools -Force | Out-Null

function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Get-File([string]$Url, [string]$Out) {
    Write-Host "    downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing -TimeoutSec 600
}

try {
    # ---------------------------------------------------------------- Python (embeddable)
    $pyExe = Join-Path $pyDir 'python.exe'
    $current = if (Test-Path $pyExe) { (& $pyExe -c "import platform; print(platform.python_version())" 2>$null) } else { '' }
    if ($Force -or $current -ne $PythonVersion) {
        Step "Python $PythonVersion (embeddable, 64-bit)"
        if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
        $zip = Join-Path $tmp 'python.zip'
        Get-File "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" $zip
        Expand-Archive -Path $zip -DestinationPath $pyDir -Force

        # The embeddable package ignores site-packages until 'import site' is enabled in pythonXY._pth
        $pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
        $lines = Get-Content $pth.FullName | ForEach-Object { if ($_ -match '^\s*#\s*import site') { 'import site' } else { $_ } }
        if ($lines -notcontains 'Lib\site-packages') { $lines = @($lines[0], 'Lib\site-packages') + $lines[1..($lines.Count - 1)] }
        if ($lines -notcontains '..\..') { $lines += '..\..' }  # the Pack-A-Lot folder, so 'import common' works
        Set-Content -Path $pth.FullName -Value $lines -Encoding ascii
    }
    else {
        Step "Python $PythonVersion already present"
    }

    # ---------------------------------------------------------------- pip + packages
    Step 'Python packages (requirements.txt)'
    if (-not (Test-Path (Join-Path $pyDir 'Lib\site-packages\pip'))) {
        $getPip = Join-Path $tmp 'get-pip.py'
        Get-File 'https://bootstrap.pypa.io/get-pip.py' $getPip
        & $pyExe $getPip --no-cache-dir --no-warn-script-location --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { throw 'pip bootstrap failed' }
    }
    & $pyExe -m pip install --no-cache-dir --no-warn-script-location --disable-pip-version-check --upgrade -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    & $pyExe -c "from importlib.metadata import version as v; print('    flask', v('flask'), '/ pyyaml', v('pyyaml'))"

    # ---------------------------------------------------------------- IntuneWinAppUtil
    $iw = Join-Path $tools 'IntuneWinAppUtil.exe'
    if ($Force -or -not (Test-Path $iw)) {
        Step 'IntuneWinAppUtil.exe'
        Get-File 'https://github.com/microsoft/Microsoft-Win32-Content-Prep-Tool/raw/master/IntuneWinAppUtil.exe' "$iw.download"
        Move-Item "$iw.download" $iw -Force
    }
    else {
        Step 'IntuneWinAppUtil.exe already present'
    }

    $size = (Get-ChildItem $runtime, $tools -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
    Write-Host ''
    Write-Host ("Done. runtime\ + tools\ = {0:N0} MB. Copy the whole Pack-A-Lot folder to the other PC and run Setup.cmd there." -f $size) -ForegroundColor Green
}
finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
