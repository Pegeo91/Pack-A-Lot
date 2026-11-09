<#
.SYNOPSIS
    Runs INSIDE Windows Sandbox. Installs the PSADT package, evaluates the Intune detection rules,
    uninstalls, evaluates again, and writes results to C:\PackALotTest\results\result.json.

    Mapped folders (configured by tester.py):
      C:\PackALotPkg   - the built PSADT package (read-only)
      C:\PackALotTest  - this script, test-config.json and the results folder (read/write)
#>
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Root = 'C:\PackALotTest'
$Results = Join-Path $Root 'results'
$PkgSource = 'C:\PackALotPkg'
$PkgLocal = 'C:\Pkg'
New-Item -ItemType Directory -Path $Results -Force | Out-Null
$ProgressLog = Join-Path $Results 'progress.log'

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message
    Add-Content -LiteralPath $ProgressLog -Value $line -Encoding UTF8
    Write-Host $line
}

# --------------------------------------------------------------------------- live log
# A background runspace follows the PSADT / MSI log files while they are written and appends new lines to
# results\live.log, which the host shows live in the browser. It is the only writer of live.log; the main
# script hands it phase markers through a queue.
$LiveLog = Join-Path $Results 'live.log'
$LiveSync = [hashtable]::Synchronized(@{ Stop = $false; Queue = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]' })

function Start-LiveLog([string[]]$Dirs = @("$env:windir\Logs\Software", "$env:windir\Logs\PSAppDeployToolkit",
                                             "$env:ProgramData\Logs\Software")) {
    $rs = [runspacefactory]::CreateRunspace()
    $rs.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript({
        param($Sync, $Out, $Dirs)
        $enc = New-Object Text.UTF8Encoding($false)
        $writer = New-Object IO.StreamWriter($Out, $true, $enc)
        $writer.AutoFlush = $true
        $state = @{}      # file -> @{ Pos; Encoding }
        $current = ''
        while ($true) {
            $item = $null
            while ($Sync.Queue.TryDequeue([ref]$item)) { $writer.WriteLine("@@PHASE $item"); $current = '' }
            foreach ($dir in $Dirs) {
                if (-not (Test-Path -LiteralPath $dir)) { continue }
                foreach ($f in Get-ChildItem -LiteralPath $dir -Recurse -File -Include *.log -ErrorAction SilentlyContinue) {
                    try {
                        $fs = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite, Delete')
                        try {
                            if (-not $state.ContainsKey($f.FullName)) {
                                $bom = New-Object byte[] 3
                                $n = $fs.Read($bom, 0, 3)
                                $isUtf16 = ($n -ge 2 -and $bom[0] -eq 0xFF -and $bom[1] -eq 0xFE)
                                $isUtf8Bom = ($n -eq 3 -and $bom[0] -eq 0xEF -and $bom[1] -eq 0xBB -and $bom[2] -eq 0xBF)
                                $state[$f.FullName] = @{ Pos = [long]$(if ($isUtf16) { 2 } elseif ($isUtf8Bom) { 3 } else { 0 })
                                                         Encoding = $(if ($isUtf16) { [Text.Encoding]::Unicode } else { [Text.Encoding]::UTF8 }) }
                            }
                            $st = $state[$f.FullName]
                            if ($fs.Length -lt $st.Pos) { $st.Pos = 0 }  # file was recreated
                            if ($fs.Length -le $st.Pos) { continue }
                            $fs.Position = $st.Pos
                            $buf = New-Object byte[] ([int][math]::Min($fs.Length - $st.Pos, 4MB))
                            $read = $fs.Read($buf, 0, $buf.Length)
                            $text = $st.Encoding.GetString($buf, 0, $read)
                            $cut = $text.LastIndexOf("`n")
                            if ($cut -lt 0) { continue }  # wait until a whole line is written
                            $chunk = $text.Substring(0, $cut + 1)
                            $st.Pos += $st.Encoding.GetByteCount($chunk)
                            if ($current -ne $f.FullName) { $writer.WriteLine("@@FILE $($f.Name)"); $current = $f.FullName }
                            foreach ($line in $chunk.TrimEnd("`r", "`n") -split "`r?`n") { $writer.WriteLine($line) }
                        }
                        finally { $fs.Dispose() }
                    }
                    catch { }
                }
            }
            if ($Sync.Stop) { break }
            Start-Sleep -Milliseconds 700
        }
        $writer.Dispose()
    }).AddArgument($LiveSync).AddArgument($LiveLog).AddArgument($Dirs)
    $script:LiveHandle = @{ PS = $ps; Async = $ps.BeginInvoke() }
}

function Stop-LiveLog {
    if (-not $script:LiveHandle) { return }
    $LiveSync.Stop = $true
    try { [void]$script:LiveHandle.Async.AsyncWaitHandle.WaitOne(5000) } catch { }
    try { $script:LiveHandle.PS.Dispose() } catch { }
}

# --------------------------------------------------------------------------- helpers
function Get-UninstallEntries {
    $roots = @(
        @{ Hive = 'HKLM'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' },
        @{ Hive = 'HKLM32'; Path = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall' },
        @{ Hive = 'HKCU'; Path = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' }
    )
    foreach ($r in $roots) {
        if (-not (Test-Path $r.Path)) { continue }
        foreach ($k in Get-ChildItem -LiteralPath $r.Path -ErrorAction SilentlyContinue) {
            $p = Get-ItemProperty -LiteralPath $k.PSPath -ErrorAction SilentlyContinue
            if (-not $p.DisplayName) { continue }
            [pscustomobject]@{
                KeyPath              = ($k.Name -replace '^HKEY_LOCAL_MACHINE', 'HKEY_LOCAL_MACHINE' -replace '^HKEY_CURRENT_USER', 'HKEY_CURRENT_USER')
                KeyName              = $k.PSChildName
                DisplayName          = "$($p.DisplayName)"
                DisplayVersion       = "$($p.DisplayVersion)"
                Publisher            = "$($p.Publisher)"
                InstallLocation      = "$($p.InstallLocation)"
                UninstallString      = "$($p.UninstallString)"
                QuietUninstallString = "$($p.QuietUninstallString)"
                WindowsInstaller     = [bool]($p.WindowsInstaller -eq 1)
                Is32Bit              = ($r.Hive -eq 'HKLM32')
            }
        }
    }
}

function ConvertTo-Comparable($Value, [string]$Kind) {
    switch ($Kind) {
        'version' { try { return [version](("$Value" -replace '[^0-9.].*$', '').Trim('.')) } catch { return $null } }
        'integer' { try { return [int64]$Value } catch { return $null } }
        'date'    { try { return ([datetime]$Value).ToUniversalTime() } catch { return $null } }
        'number'  { try { return [double]$Value } catch { return $null } }
        default   { return "$Value" }
    }
}

function Test-Operator($Actual, [string]$Operator, $Expected, [string]$Kind) {
    if (-not $Operator -or $Operator -eq 'notConfigured') { return $true }
    $a = ConvertTo-Comparable $Actual $Kind
    $e = ConvertTo-Comparable $Expected $Kind
    if ($null -eq $a -or $null -eq $e) { return $false }
    switch ($Operator) {
        'equal'              { return $a -eq $e }
        'notEqual'           { return $a -ne $e }
        'greaterThan'        { return $a -gt $e }
        'greaterThanOrEqual' { return $a -ge $e }
        'lessThan'           { return $a -lt $e }
        'lessThanOrEqual'    { return $a -le $e }
    }
    return $false
}

function Expand-IntunePath([string]$Path, [bool]$Check32) {
    if ($Check32) {
        $Path = $Path -replace '%ProgramFiles%', '%ProgramFiles(x86)%' -replace '%CommonProgramFiles%', '%CommonProgramFiles(x86)%'
        $Path = $Path -replace '(?i)C:\\Program Files\\', 'C:\Program Files (x86)\'
    }
    return [Environment]::ExpandEnvironmentVariables($Path)
}

function Test-DetectionRule($Rule) {
    $detected = $false; $detail = ''
    try {
        switch ($Rule.type) {
            'msi' {
                $code = "$($Rule.product_code)".Trim()
                $key = @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$code",
                         "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$code") |
                       Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
                if (-not $key) { $detail = "Product code $code not registered"; break }
                $ver = (Get-ItemProperty -LiteralPath $key).DisplayVersion
                $detected = Test-Operator $ver $Rule.version_operator $Rule.version 'version'
                $detail = "Product code $code found, version $ver"
            }
            'file' {
                $folder = Expand-IntunePath $Rule.path ([bool]$Rule.check32)
                $full = Join-Path $folder $Rule.name
                $exists = Test-Path -LiteralPath $full
                switch ($Rule.detection_type) {
                    'exists'       { $detected = $exists }
                    'doesNotExist' { $detected = -not $exists }
                    'version'      { if ($exists) { $v = (Get-Item -LiteralPath $full).VersionInfo.FileVersionRaw; $detected = Test-Operator $v $Rule.operator $Rule.value 'version'; $detail = "File version $v" } }
                    'sizeInMB'     { if ($exists) { $s = [math]::Round((Get-Item -LiteralPath $full).Length / 1MB, 2); $detected = Test-Operator $s $Rule.operator $Rule.value 'number'; $detail = "Size $s MB" } }
                    'modifiedDate' { if ($exists) { $d = (Get-Item -LiteralPath $full).LastWriteTimeUtc; $detected = Test-Operator $d $Rule.operator $Rule.value 'date'; $detail = "Modified $d" } }
                    'createdDate'  { if ($exists) { $d = (Get-Item -LiteralPath $full).CreationTimeUtc; $detected = Test-Operator $d $Rule.operator $Rule.value 'date'; $detail = "Created $d" } }
                }
                $detail = "$full exists=$exists. $detail"
            }
            'registry' {
                $keyPath = "$($Rule.key)".Trim()
                $hiveName, $sub = $keyPath -split '\\', 2
                $hive = switch -Regex ($hiveName) {
                    '^(HKEY_LOCAL_MACHINE|HKLM)$' { [Microsoft.Win32.RegistryHive]::LocalMachine }
                    '^(HKEY_CURRENT_USER|HKCU)$'  { [Microsoft.Win32.RegistryHive]::CurrentUser }
                    '^(HKEY_CLASSES_ROOT|HKCR)$'  { [Microsoft.Win32.RegistryHive]::ClassesRoot }
                    '^(HKEY_USERS|HKU)$'          { [Microsoft.Win32.RegistryHive]::Users }
                    default { throw "Unknown registry hive '$hiveName'" }
                }
                $view = if ($Rule.check32) { [Microsoft.Win32.RegistryView]::Registry32 } else { [Microsoft.Win32.RegistryView]::Registry64 }
                $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
                $key = $base.OpenSubKey($sub)
                $name = "$($Rule.value_name)"
                $exists = if (-not $key) { $false } elseif ($name) { $key.GetValueNames() -contains $name } else { $true }
                $value = if ($key -and $name) { $key.GetValue($name) } else { $null }
                switch ($Rule.detection_type) {
                    'exists'       { $detected = $exists }
                    'doesNotExist' { $detected = -not $exists }
                    'string'       { $detected = $exists -and (Test-Operator $value $Rule.operator $Rule.value 'string') }
                    'integer'      { $detected = $exists -and (Test-Operator $value $Rule.operator $Rule.value 'integer') }
                    'version'      { $detected = $exists -and (Test-Operator $value $Rule.operator $Rule.value 'version') }
                }
                $detail = "$keyPath [$name] exists=$exists value=$value"
            }
            'script' {
                $file = Join-Path $env:TEMP ("detect_{0}.ps1" -f [guid]::NewGuid().ToString('N'))
                [IO.File]::WriteAllText($file, "$($Rule.content)", (New-Object Text.UTF8Encoding($true)))
                $ps = if ($Rule.run32) { "$env:windir\SysWOW64\WindowsPowerShell\v1.0\powershell.exe" } else { "$env:windir\System32\WindowsPowerShell\v1.0\powershell.exe" }
                $out = & $ps -NoProfile -ExecutionPolicy Bypass -File $file 2>&1 | Out-String
                $code = $LASTEXITCODE
                Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
                $detected = ($code -eq 0) -and ($out.Trim().Length -gt 0)
                $detail = "Exit code $code, output: $($out.Trim())"
            }
            default { $detail = "Unknown rule type $($Rule.type)" }
        }
    }
    catch { $detail = "Error: $($_.Exception.Message)" }
    [pscustomobject]@{ type = $Rule.type; detected = [bool]$detected; detail = $detail }
}

function Test-Detection([string]$StepName, [bool]$ExpectDetected) {
    $ruleResults = @(foreach ($r in $Config.detection_rules) { Test-DetectionRule $r })
    $detected = ($ruleResults.Count -gt 0) -and -not ($ruleResults | Where-Object { -not $_.detected })
    foreach ($rr in $ruleResults) { Write-Log ("  [{0}] detected={1} - {2}" -f $rr.type, $rr.detected, $rr.detail) }
    $ok = ($detected -eq $ExpectDetected)
    Write-Log ("{0}: detected={1} (expected {2}) -> {3}" -f $StepName, $detected, $ExpectDetected, $(if ($ok) { 'PASS' } else { 'FAIL' }))
    [pscustomobject]@{ name = $StepName; kind = 'detection'; detected = [bool]$detected; expected = $ExpectDetected; ok = $ok; rules = $ruleResults }
}

function Invoke-DeploymentStep([string]$StepName, [string]$Command) {
    Write-Log "$StepName > $Command"
    $LiveSync.Queue.Enqueue($StepName)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    # Not 'Start-Process -Wait': in Windows PowerShell that waits for every process the installer started, so an
    # installer that leaves something running (an updater, the app itself) would hang the test until the timeout.
    $p = Start-Process -FilePath "$env:ComSpec" -ArgumentList "/s /c `"$Command`"" -WorkingDirectory $PkgLocal -PassThru -NoNewWindow
    [void]$p.Handle  # keep the process handle so ExitCode is available after exit
    $p.WaitForExit()
    $sw.Stop()
    $code = $p.ExitCode
    $type = ($Config.return_codes | Where-Object { [int]$_.code -eq $code } | Select-Object -First 1).type
    $ok = $type -in @('success', 'softReboot', 'hardReboot')
    Write-Log ("{0}: exit code {1} ({2}) in {3:N0}s -> {4}" -f $StepName, $code, $(if ($type) { $type } else { 'unknown' }), $sw.Elapsed.TotalSeconds, $(if ($ok) { 'PASS' } else { 'FAIL' }))
    [pscustomobject]@{ name = $StepName; kind = 'deployment'; command = $Command; exit_code = $code; code_type = "$type"; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); ok = $ok }
}

# --------------------------------------------------------------------------- main
$result = [ordered]@{ success = $false; error = $null; steps = @(); new_entries = @(); leftover_entries = @(); started = (Get-Date).ToString('s') }
try {
    $Config = Get-Content -LiteralPath (Join-Path $Root 'test-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Log 'Pack-A-Lot sandbox test started.'
    Write-Log "Copying package to $PkgLocal"
    Copy-Item -LiteralPath $PkgSource -Destination $PkgLocal -Recurse -Force
    Get-ChildItem -LiteralPath $PkgLocal -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

    $before = @(Get-UninstallEntries)
    $steps = New-Object System.Collections.ArrayList
    Start-LiveLog

    [void]$steps.Add((Invoke-DeploymentStep 'Install' $Config.install_command))
    $afterInstall = @(Get-UninstallEntries)
    $newEntries = @($afterInstall | Where-Object { $_.KeyPath -notin $before.KeyPath })
    foreach ($e in $newEntries) { Write-Log "New uninstall entry: $($e.DisplayName) $($e.DisplayVersion) [$($e.KeyPath)]" }
    $result.new_entries = $newEntries
    [void]$steps.Add((Test-Detection 'Detection after install' $true))

    if ($Config.test_uninstall) {
        $uninstallStep = Invoke-DeploymentStep 'Uninstall' $Config.uninstall_command
        [void]$steps.Add($uninstallStep)
        $afterUninstall = @(Get-UninstallEntries)
        $result.leftover_entries = @($afterUninstall | Where-Object { $_.KeyPath -in $newEntries.KeyPath })
        foreach ($e in $result.leftover_entries) { Write-Log "WARNING: still registered after uninstall: $($e.DisplayName)" }
        # An exit code of 0 is not proof: PSADT exits 0 when Uninstall-ADTApplication finds no matching app
        $note = ''
        if ($newEntries.Count -gt 0 -and $result.leftover_entries.Count -eq $newEntries.Count) {
            $uninstallStep.ok = $false
            $note = 'Exit code was OK, but nothing was removed - the uninstall code probably did not match the installed app (check the name in the Uninstall section against the registered names below).'
            Write-Log "Uninstall: $note -> FAIL"
        }
        elseif ($result.leftover_entries.Count -gt 0) {
            $note = 'Some components are still registered: ' + (($result.leftover_entries | ForEach-Object { $_.DisplayName }) -join ', ') + '. Add them to the Uninstall section if they should be removed.'
        }
        $uninstallStep | Add-Member -NotePropertyName note -NotePropertyValue $note
        [void]$steps.Add((Test-Detection 'Detection after uninstall' $false))
    }

    $result.steps = $steps
    $result.success = -not ($steps | Where-Object { -not $_.ok })
}
catch {
    $result.error = "$($_.Exception.Message)"
    Write-Log "ERROR: $($result.error)"
}
finally {
    Stop-LiveLog
    # Collect PSADT / MSI logs
    $logDest = Join-Path $Results 'logs'
    New-Item -ItemType Directory -Path $logDest -Force | Out-Null
    foreach ($dir in "$env:windir\Logs\Software", "$env:windir\Logs\PSAppDeployToolkit") {
        if (Test-Path $dir) { Copy-Item -Path "$dir\*" -Destination $logDest -Recurse -Force -ErrorAction SilentlyContinue }
    }
    $result.logs = @(Get-ChildItem -LiteralPath $logDest -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName.Substring($logDest.Length + 1) })
    $result.finished = (Get-Date).ToString('s')
    Write-Log ("Test finished: {0}" -f $(if ($result.success) { 'SUCCESS' } else { 'FAILED' }))
    $json = $result | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText((Join-Path $Results 'result.json'), $json, (New-Object Text.UTF8Encoding($false)))

    if (-not $Config.keep_open) {
        Write-Log 'Shutting down sandbox in 5 seconds...'
        Start-Sleep -Seconds 5
        Stop-Computer -Force
    }
    else {
        Write-Log 'Sandbox left open for inspection (close the window when done).'
    }
}
