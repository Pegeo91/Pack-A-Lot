# Pack-A-Lot

Browser-based wizard that packages Win32 apps with **PSAppDeployToolkit v4**, tests them in
**Windows Sandbox**, and uploads them to **Intune** through the Microsoft Graph PowerShell SDK.

```
Source ─► App details ─► PSADT script ─► Intune settings ─► Build ─► Sandbox test ─► Upload
(winget or                (on/off switches  (command switches,  (.intunewin) (install/detect/  (Connect-MgGraph)
 own file)                 + 6 code sections) detection rules)                uninstall/detect)
```

## Quick start

Double-click **`Pack-A-Lot.exe`**. It starts the server in a console window and opens
http://127.0.0.1:5050. Close the console window to stop it. Launching it a second time just opens the browser.

* The exe includes Python and Flask, but it uses the `static\`, `scripts\` and `PSADT Template\`
  folders and `config.yaml` next to it, so **keep it in this folder**.
* To rebuild the exe after changing the Python code, run `build_exe.cmd` (uses PyInstaller).
* Or run from source: `pip install -r requirements.txt` then `python app.py` (or `start.cmd`).

## Intune app registration: `config.yaml`

Created on first start, next to the exe. Fill in your app registration:

```yaml
intune:
  auth_mode: "certificate"        # interactive | devicecode | secret | certificate
  tenant_id: "contoso.onmicrosoft.com"
  client_id: "00000000-0000-0000-0000-000000000000"
  cert_thumbprint: "A1B2C3..."
```

The Upload step loads these values, and any changes you make there are saved back to the file.
A client secret is never written to disk. Type it in the UI or set `PACKALOT_CLIENT_SECRET`.

### Required permissions

| Auth mode | Microsoft Graph permission | Also needed |
|---|---|---|
| `certificate` / `secret` (app-only) | **Application**: `DeviceManagementApps.ReadWrite.All` + admin consent | Certificate public key (.cer) uploaded to the app registration, private key in `Cert:\CurrentUser\My` |
| `interactive` / `devicecode` (your own client ID) | **Delegated**: `DeviceManagementApps.ReadWrite.All` + admin consent | Redirect URI `http://localhost` (Mobile & desktop platform); *Allow public client flows* = Yes for device code. The signed-in user needs the Intune role **Intune Administrator** or **Application Manager** |
| `interactive` without a client ID | Uses the Microsoft Graph Command Line Tools app | Someone must consent to `DeviceManagementApps.ReadWrite.All` once, and the user needs the Intune role above |

This single permission covers creating the app, the content upload (Azure Storage SAS URLs are issued
by Intune) and assignments to groups entered by object ID.

Assignment groups can also be entered by their **display name**. Pack-A-Lot then looks them up after
sign-in, which additionally needs `Group.Read.All` (Application for app-only, Delegated for
interactive/device code; admin consent). The permission is only requested when a name is used. The lookup
runs before anything is created, so a name that is missing or not unique stops the upload cleanly.

### Requirements
| Need | Why |
|---|---|
| Windows 10/11 Pro/Enterprise, Python 3.10+ | |
| `winget` | winget sources |
| Windows Sandbox optional feature | Sandbox testing |
| `IntuneWinAppUtil.exe` | Downloaded automatically into `tools\` on first build |
| `Microsoft.Graph.Authentication` module | Installed automatically (CurrentUser) on first upload |
| Graph permission `DeviceManagementApps.ReadWrite.All` | Upload + assignments |

## Project layout

| File | Purpose |
|---|---|
| `app.py` | Flask web server, REST API, background task runner |
| `packager.py` | Fetch the installer (winget / own file) and read its metadata; build the PSADT package and `.intunewin` |
| `tester.py` | Generates the `.wsb`, launches Windows Sandbox, follows progress, collects results |
| `uploader.py` | Builds the Graph `win32LobApp` body (detection, requirements, return codes, assignments) and runs the upload |
| `psadt_script.py` | Generates `Invoke-AppDeployToolkit.ps1` from the template + switches + your section code |
| `winget_client.py` | winget search / versions / download + manifest parsing |
| `common.py` | Settings, job storage, process helpers |
| `scripts/Test-Package.ps1` | Runs **inside** the sandbox: install → detection → uninstall → detection, registry diff |
| `scripts/Upload-Win32App.ps1` | `Connect-MgGraph` + `Invoke-MgGraphRequest` chunked encrypted upload |
| `scripts/Get-InstallerInfo.ps1` | Reads MSI properties / EXE version info |
| `static/` | Wizard UI (vanilla HTML/CSS/JS, no build step) |
| `PSADT Template/` | Your PSADT v4 template, copied for every package |

Each step can also be run without the UI:

```powershell
python packager.py <job-id> fetch
python packager.py <job-id> build
python tester.py   <job-id>
python uploader.py <job-id>
```

Jobs (installer, package, `.intunewin`, test results, logs) live in
`%LOCALAPPDATA%\PackALot\workspace\<job-id>`, outside OneDrive. You can change this in **Settings**.

## Moving Pack-A-Lot to another PC

1. **On a PC with internet access:** run `Prepare-Portable.ps1`. It downloads into the Pack-A-Lot folder:
   - `runtime\python\`: the official Python embeddable package (3.14.5) with Flask and PyYAML, used by `start.cmd`.
   - `tools\IntuneWinAppUtil.exe`.

   `Pack-A-Lot.exe` already contains Python, so it doesn't need `runtime\`. The portable Python lets you run from
   source (`start.cmd`) on a PC that has no Python installed.
2. **Copy the whole folder** to the new PC: `Pack-A-Lot.exe`, `runtime\`, `tools\`, `scripts\`, `static\`,
   `PSADT Template\` and `config.yaml`. Paths in `settings.json` are portable: if a saved path doesn't exist on
   the new PC, the copy next to the program is used.
3. **On the new PC, run `Setup.cmd`.** It removes the "downloaded from the internet" block from the files and
   checks the following, offering to fix what can be fixed:

| Check | Fix |
|---|---|
| Windows edition (Pro/Enterprise/Education, build 18305+) | none; Home can't run Windows Sandbox |
| CPU virtualization (VT-x / AMD-V, or nested virtualization in a VM) | none; enable it in BIOS/UEFI or on the VM host |
| Windows Sandbox feature | enables `Containers-DisposableClientVM` (UAC prompt, then restart) |
| winget | registers App Installer, or opens its Microsoft Store page |
| PowerShell script policy (GPO `Restricted`/`AllSigned`, Constrained Language) | none; needs an exception from your admin |
| Microsoft.Graph.Authentication | installs it for the current user |
| IntuneWinAppUtil.exe, PSADT template, Python runtime | downloads IntuneWinAppUtil |
| `config.yaml` (IDs; for certificate auth, that the cert and its private key are on this PC) | none; import the .pfx |
| Network: winget CDN, GitHub, PowerShell Gallery, login.microsoftonline.com, graph.microsoft.com | none; firewall/proxy |

The same checks appear in the app under **System check** on the home page, with **Fix** buttons, **Re-check** and
**Test Intune connection**. The connection test signs in with the auth mode from `config.yaml` and checks that Intune
apps can be read (`DeviceManagementApps.ReadWrite.All`) and whether groups can be looked up by name (`Group.Read.All`).

## Saved customisations for new versions

When a package is built, Pack-A-Lot saves what you added or changed for that app, keyed by its winget ID
(or vendor + name for own installers). That covers the Pre/Post sections, any Install/Uninstall code you edited,
the dialog switches, processes to close, extra files and the custom package folder. The next time you package the
same app, for example a newer version, these are applied automatically after the download. In a customised
Install/Uninstall section, the old installer file name is replaced with the new one. Saved profiles are stored in
`%LOCALAPPDATA%\PackALot\profiles\` and can be removed in **Settings**. Turn off *Remember my custom sections*
in the PSADT script step to stop saving for one package.

## Custom package folder

Set **Custom package folder** in the Build step, or a default for new packages in **Settings**. After each build,
the PSADT package and the `.intunewin` are also copied to `<folder>\<App>\<Version>\`. A UNC path such as
`\server\Packages` works too.

## How the generated script works

`$adtSession` (vendor, name, version, processes to close…) is filled in, and the
`Install-ADTDeployment` / `Uninstall-ADTDeployment` functions are regenerated from:

* **Switches**: welcome dialog, deferral, disk-space check, progress, completion prompt, close-apps countdown
* **Your code** for Pre-Install / Install / Post-Install / Pre-Uninstall / Uninstall / Post-Uninstall

The rest of the template (initialisation, `Repair-ADTDeployment`, error handling) is kept as-is.
Default Install/Uninstall code is generated from the installer type (MSI / NSIS / Inno / Burn / MSIX / EXE).
"Reset to default" restores it.

## Sandbox test

The package folder is mapped read-only and copied to `C:\Pkg` inside the sandbox. The Intune
install command runs there, then **your Intune detection rules** are evaluated the way Intune
does it (MSI, file, registry, script). Next the uninstall runs and detection is checked again.
New Uninstall-registry entries are captured, so you can turn one into a detection rule with one click.
PSADT logs are copied back to `test\results\logs`.

## Upload notes
* **Device code**: the sign-in code appears in the upload log.
* If an upload fails before the content is committed, the half-created app is deleted from Intune.
