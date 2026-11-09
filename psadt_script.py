"""Generates Invoke-AppDeployToolkit.ps1 (PSADT v4) from the template + wizard state.

Also provides the default install / uninstall code and detection rules for an installer,
and the Intune install / uninstall command lines built from the on/off switches.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from common import ps_quote

SETUP_FILE = "Invoke-AppDeployToolkit.exe"

# Silent switches per winget installer technology (used when the manifest has none)
DEFAULT_SILENT = {
    "nullsoft": "/S",
    "inno": "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-",
    "burn": "/quiet /norestart",
    "wix": "",
    "msi": "",
    "exe": "/S",
}
DEFAULT_UNINSTALL_ARGS = {
    "nullsoft": "/S",
    "inno": "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART",
    "burn": "/uninstall /quiet /norestart",
    "exe": "/S",
}


# --------------------------------------------------------------------------- installer classification
def installer_kind(installer: dict) -> str:
    """Normalise to: msi | exe | msix | zip | portable."""
    wtype = (installer.get("winget_type") or "").lower()
    ext = Path(installer.get("file", "")).suffix.lower()
    if wtype in ("msi", "wix") or ext == ".msi":
        return "msi"
    if wtype in ("msix", "appx") or ext in (".msix", ".msixbundle", ".appx", ".appxbundle"):
        return "msix"
    if wtype == "zip" or ext == ".zip":
        return "zip"
    if wtype == "portable":
        return "portable"
    return "exe"


def default_silent_args(installer: dict, scope: str = "machine") -> str:
    wtype = (installer.get("winget_type") or "exe").lower()
    args = installer.get("silent_switch") or DEFAULT_SILENT.get(wtype, "/S")
    if wtype == "inno" and scope == "machine" and "/ALLUSERS" not in args.upper():
        args += " /ALLUSERS"
    custom = installer.get("custom_switch") or ""
    return " ".join(x for x in (args, custom) if x).strip()


def base_name(name: str) -> str:
    """'Mozilla Firefox (x64 en-US)' -> 'Mozilla Firefox'; '7-Zip 26.06 (x64)' -> '7-Zip 26.06'.

    winget package names and Add/Remove Programs names often differ in architecture / locale
    decorations, so those are dropped. Version numbers are kept on purpose: 'Python 3.14.5 (64-bit)'
    must not become 'Python', which would also match other Python versions and the Python Launcher.
    """
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name or "")          # (x64 en-US), [64-bit]
    n = re.sub(r"\s+(x64|x86|arm64|64-bit|32-bit)$", "", n, flags=re.I)
    n = n.strip(" -_")
    return n or (name or "").strip()


def name_regex(name: str) -> str:
    """Regex for Uninstall-ADTApplication -NameMatch Regex / -match.

    The base name may only be followed by more version digits, a version number and/or bracketed
    decorations - nothing else, so a different product is never matched:
      'Mozilla Firefox'  -> 'Mozilla Firefox (x64 en-US)', 'Mozilla Firefox 159.0 (x64 en-US)'
                            but NOT 'Mozilla Firefox ESR' / 'Mozilla Maintenance Service'
      'Microsoft Edge'   -> NOT 'Microsoft Edge WebView2 Runtime';  'Git' -> NOT 'GitHub Desktop'
      'Python 3.14'      -> 'Python 3.14.5 (64-bit)' but NOT 'Python 3.13.0' / 'Python Launcher'
    Non-ASCII characters become '.' so generated scripts stay pure ASCII.
    """
    escaped = re.escape(base_name(name))
    escaped = re.sub(r"[^\x20-\x7e]", ".", escaped)
    return f"^{escaped}(\\.\\d+)*(\\s+v?\\d+(\\.\\d+)*)?(\\s*[\\(\\[][^\\)\\]]*[\\)\\]])*$"


def uninstall_by_name(display_name: str, kind: str, winget_type: str = "exe") -> str:
    regex = ps_quote(name_regex(display_name))
    if kind == "msi":
        return f"Uninstall-ADTApplication -Name {regex} -NameMatch Regex -ApplicationType MSI"
    if kind == "exe":
        args = DEFAULT_UNINSTALL_ARGS.get(winget_type or "exe", "/S")
        return f"Uninstall-ADTApplication -Name {regex} -NameMatch Regex -ApplicationType EXE -ArgumentList {ps_quote(args)}"
    return f"Uninstall-ADTApplication -Name {regex} -NameMatch Regex"


def default_sections(installer: dict, app: dict, scope: str = "machine") -> dict:
    """Default Install / Uninstall code for the PSADT script."""
    kind = installer_kind(installer)
    f = installer.get("file", "setup.exe")
    display = installer.get("display_name") or app.get("name") or installer.get("package_name") or ""
    product_code = installer.get("product_code", "")
    wtype = (installer.get("winget_type") or "exe").lower()

    if kind == "msi":
        install = f"Start-ADTMsiProcess -Action Install -FilePath {ps_quote(f)}"
        if product_code:
            uninstall = f"Start-ADTMsiProcess -Action Uninstall -ProductCode {ps_quote(product_code)}"
        else:
            uninstall = uninstall_by_name(display, "msi")
    elif kind == "msix":
        install = (f"Add-AppxProvisionedPackage -Online -PackagePath \"$($adtSession.DirFiles)\\{f}\" -SkipLicense | Out-Null")
        uninstall = ("Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -like "
                     f"{ps_quote('*' + (app.get('name') or display) + '*')} }} | "
                     "Remove-AppxProvisionedPackage -Online -AllUsers | Out-Null")
    elif kind == "zip":
        nested = (installer.get("nested_files") or [""])[0]
        install = ("## ZIP package - extract and run the nested installer (adjust as needed)\n"
                   f"Expand-Archive -LiteralPath \"$($adtSession.DirFiles)\\{f}\" -DestinationPath \"$env:TEMP\\{app.get('name') or 'app'}\" -Force\n"
                   f"# Start-ADTProcess -FilePath \"$env:TEMP\\{app.get('name') or 'app'}\\{nested or 'setup.exe'}\" -ArgumentList '/S'")
        uninstall = uninstall_by_name(display, "any")
    elif kind == "portable":
        target = f"$env:ProgramFiles\\{app.get('vendor') or 'Vendor'}\\{app.get('name') or 'App'}"
        install = (f"New-ADTFolder -LiteralPath \"{target}\"\n"
                   f"Copy-ADTFile -Path \"$($adtSession.DirFiles)\\{f}\" -Destination \"{target}\"")
        uninstall = f"Remove-ADTFolder -LiteralPath \"{target}\""
    else:
        args = default_silent_args(installer, scope)
        install = f"Start-ADTProcess -FilePath {ps_quote(f)} -ArgumentList {ps_quote(args)}"
        uninstall = uninstall_by_name(display, "exe", wtype)
    return {"install": install, "uninstall": uninstall}


def default_detection(installer: dict, app: dict) -> list[dict]:
    """MSI -> product code rule. Anything else -> registry-scan detection script."""
    kind = installer_kind(installer)
    if kind == "msi" and installer.get("product_code"):
        return [{"type": "msi", "product_code": installer["product_code"],
                 "version_operator": "notConfigured", "version": ""}]
    name = installer.get("display_name") or app.get("name") or installer.get("package_name") or "App"
    version = installer.get("display_version") or app.get("version") or ""
    return [{"type": "script", "content": detection_script(name, version),
             "run32": False, "enforce_signature": False}]


def detection_script(display_name: str, min_version: str = "") -> str:
    # Intune runs detection scripts as UTF-8 without BOM, which Windows PowerShell 5.1 misreads:
    # name_regex() keeps the script pure ASCII. Matching is on the base name, so
    # 'Mozilla Firefox (en-US)' also finds 'Mozilla Firefox (x64 en-US)'.
    min_version = re.sub(r"[^\x20-\x7e]", "", min_version)
    return f"""# Detects the app via the Uninstall registry keys (32-bit and 64-bit hives)
# Matches DisplayName '{re.sub(r"[^\x20-\x7e]", "?", base_name(display_name))}' plus optional version / (decorations)
$namePattern = {ps_quote(name_regex(display_name))}
$minVersion  = {ps_quote(min_version)}
$paths = @(
    'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
    'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
)
$app = Get-ItemProperty -Path $paths -ErrorAction SilentlyContinue |
    Where-Object {{ $_.DisplayName -match $namePattern }} | Select-Object -First 1
if (-not $app) {{ exit 1 }}
if (-not $minVersion) {{ Write-Output "Detected $($app.DisplayName)"; exit 0 }}
try {{
    if ([version]($app.DisplayVersion -replace '[^0-9.].*$','') -ge [version]($minVersion -replace '[^0-9.].*$','')) {{
        Write-Output "Detected $($app.DisplayName) $($app.DisplayVersion)"; exit 0
    }}
}} catch {{
    if ($app.DisplayVersion -eq $minVersion) {{ Write-Output "Detected $($app.DisplayName)"; exit 0 }}
}}
exit 1
"""


# --------------------------------------------------------------------------- command lines
def build_command(cmd: dict, deployment_type: str) -> str:
    """Intune install / uninstall command from the on/off switches."""
    if cmd.get("use_custom") and cmd.get("custom", "").strip():
        return cmd["custom"].strip()
    parts = [SETUP_FILE, "-DeploymentType", deployment_type]
    mode = cmd.get("deploy_mode") or "Silent"
    if mode != "Default":
        parts += ["-DeployMode", mode]
    if cmd.get("suppress_reboot"):
        parts.append("-SuppressRebootPassThru")
    if cmd.get("terminal_server"):
        parts.append("-TerminalServerMode")
    if cmd.get("disable_logging"):
        parts.append("-DisableLogging")
    return " ".join(parts)


# --------------------------------------------------------------------------- script generation
def generate(template_text: str, job: dict) -> str:
    app = job["app"]
    text = template_text

    def set_var(name: str, value_ps: str) -> None:
        nonlocal text
        pattern = re.compile(rf"^(\s*{name}\s*=\s*).*$", re.MULTILINE)
        if not pattern.search(text):
            raise ValueError(f"Template is missing the '{name}' variable in $adtSession")
        text = pattern.sub(lambda m: m.group(1) + value_ps, text, count=1)

    set_var("AppVendor", ps_quote(app.get("vendor", "")))
    set_var("AppName", ps_quote(app.get("name", "")))
    set_var("AppVersion", ps_quote(app.get("version", "")))
    set_var("AppArch", ps_quote(app.get("arch", "")))
    set_var("AppLang", ps_quote(app.get("lang", "EN")))
    set_var("AppRevision", ps_quote(app.get("revision", "01")))
    set_var("AppScriptDate", ps_quote(date.today().isoformat()))
    set_var("AppScriptAuthor", ps_quote(app.get("author", "") or "Pack-A-Lot"))
    procs = [p.strip() for p in re.split(r"[,;\n]", app.get("processes_to_close", "")) if p.strip()]
    procs = [p[:-4] if p.lower().endswith(".exe") else p for p in procs]
    set_var("AppProcessesToClose", "@(" + ", ".join(ps_quote(p) for p in procs) + ")")

    i_start = text.find("function Install-ADTDeployment")
    u_start = text.find("function Uninstall-ADTDeployment")
    r_start = text.find("function Repair-ADTDeployment")
    if min(i_start, u_start, r_start) < 0 or not i_start < u_start < r_start:
        raise ValueError("Template does not contain the expected Install/Uninstall/Repair functions")

    return (text[:i_start] + _install_function(job) + "\n\n" + _uninstall_function(job) + "\n\n" + text[r_start:])


def _indent(code: str, spaces: int = 4) -> str:
    pad = " " * spaces
    lines = (code or "").replace("\r\n", "\n").strip("\n").split("\n")
    return "\n".join((pad + ln) if ln.strip() else "" for ln in lines)


def _block(title: str) -> str:
    return ("    ##================================================\n"
            f"    ## MARK: {title}\n"
            "    ##================================================\n")


def _user(code: str, placeholder: str) -> str:
    code = (code or "").strip()
    if not code:
        return f"    ## <{placeholder}>\n"
    return f"    ## --- Custom code (Pack-A-Lot) ---\n{_indent(code)}\n"


def _install_function(job: dict) -> str:
    t = job["script"]["toggles"]
    s = job["script"]["sections"]
    out = ["function Install-ADTDeployment\n{\n    [CmdletBinding()]\n    param\n    (\n    )\n\n",
           _block("Pre-Install"),
           '    $adtSession.InstallPhase = "Pre-$($adtSession.DeploymentType)"\n\n']
    if t.get("welcome"):
        params = []
        if t.get("allow_defer"):
            params += ["        AllowDefer = $true", f"        DeferTimes = {int(t.get('defer_times') or 3)}"]
        if t.get("check_disk_space"):
            params.append("        CheckDiskSpace = $true")
        if t.get("persist_prompt"):
            params.append("        PersistPrompt = $true")
        out.append("    ## Show Welcome Message (close processes / deferral / disk space check).\n")
        out.append("    $saiwParams = @{\n" + "\n".join(params) + ("\n" if params else "") + "    }\n")
        out.append("    if ($adtSession.AppProcessesToClose.Count -gt 0)\n    {\n"
                   "        $saiwParams.Add('CloseProcesses', $adtSession.AppProcessesToClose)\n    }\n")
        out.append("    Show-ADTInstallationWelcome @saiwParams\n\n")
    if t.get("progress"):
        out.append("    ## Show Progress Message (with the default message).\n    Show-ADTInstallationProgress\n\n")
    out.append(_user(s.get("pre_install"), "Perform Pre-Installation tasks here"))
    out.append("\n\n" + _block("Install"))
    out.append("    $adtSession.InstallPhase = $adtSession.DeploymentType\n\n")
    out.append(_user(s.get("install"), "Perform Installation tasks here"))
    out.append("\n\n" + _block("Post-Install"))
    out.append('    $adtSession.InstallPhase = "Post-$($adtSession.DeploymentType)"\n\n')
    out.append(_user(s.get("post_install"), "Perform Post-Installation tasks here"))
    if t.get("completion_prompt"):
        msg = t.get("completion_message") or "Installation completed successfully."
        out.append(f"\n    ## Display a message at the end of the install.\n"
                   f"    Show-ADTInstallationPrompt -Message {ps_quote(msg)} -ButtonRightText 'OK' -NoWait\n")
    out.append("}")
    return "".join(out)


def _uninstall_function(job: dict) -> str:
    t = job["script"]["toggles"]
    s = job["script"]["sections"]
    out = ["function Uninstall-ADTDeployment\n{\n    [CmdletBinding()]\n    param\n    (\n    )\n\n",
           _block("Pre-Uninstall"),
           '    $adtSession.InstallPhase = "Pre-$($adtSession.DeploymentType)"\n\n']
    if t.get("uninstall_welcome"):
        out.append("    ## If there are processes to close, show Welcome Message with a countdown before automatically closing.\n"
                   "    if ($adtSession.AppProcessesToClose.Count -gt 0)\n    {\n"
                   f"        Show-ADTInstallationWelcome -CloseProcesses $adtSession.AppProcessesToClose "
                   f"-CloseProcessesCountdown {int(t.get('close_countdown') or 60)}\n    }}\n\n")
    if t.get("uninstall_progress"):
        out.append("    ## Show Progress Message (with the default message).\n    Show-ADTInstallationProgress\n\n")
    out.append(_user(s.get("pre_uninstall"), "Perform Pre-Uninstallation tasks here"))
    out.append("\n\n" + _block("Uninstall"))
    out.append("    $adtSession.InstallPhase = $adtSession.DeploymentType\n\n")
    out.append(_user(s.get("uninstall"), "Perform Uninstallation tasks here"))
    out.append("\n\n" + _block("Post-Uninstallation"))
    out.append('    $adtSession.InstallPhase = "Post-$($adtSession.DeploymentType)"\n\n')
    out.append(_user(s.get("post_uninstall"), "Perform Post-Uninstallation tasks here"))
    out.append("}")
    return "".join(out)
