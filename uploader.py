"""Uploads the .intunewin to Intune as a Win32 app via the Microsoft Graph PowerShell SDK.

Python builds the Graph request bodies (app, detection rules, requirements, return codes,
assignments); scripts/Upload-Win32App.ps1 authenticates with Connect-MgGraph and performs the
encrypted chunked upload with Invoke-MgGraphRequest.

CLI usage:
    python uploader.py <job-id>      # uses the app registration from config.yaml
                                       (secret auth: set PACKALOT_CLIENT_SECRET first)
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import psadt_script
from common import (SCRIPTS_DIR, LogFn, capture, job_dir, load_auth_config, load_job, load_settings, powershell_cmd,
                    run_process, save_job)

FILE_TYPES = {"exists", "doesNotExist", "version", "sizeInMB", "modifiedDate", "createdDate"}
REG_TYPES = {"exists", "doesNotExist", "string", "integer", "version"}


def graph_module_installed() -> bool:
    code, out = capture(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                         "if (Get-Module -ListAvailable Microsoft.Graph.Authentication) { 'yes' } else { 'no' }"], timeout=60)
    return "yes" in out


# --------------------------------------------------------------------------- request bodies
def detection_rules_body(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        t = r.get("type")
        if t == "msi":
            op = r.get("version_operator") or "notConfigured"
            out.append({
                "@odata.type": "#microsoft.graph.win32LobAppProductCodeDetection",
                "productCode": r.get("product_code", "").strip(),
                "productVersionOperator": op,
                "productVersion": (r.get("version") or None) if op != "notConfigured" else None,
            })
        elif t == "file":
            dt = r.get("detection_type") or "exists"
            out.append({
                "@odata.type": "#microsoft.graph.win32LobAppFileSystemDetection",
                "path": r.get("path", "").strip(),
                "fileOrFolderName": r.get("name", "").strip(),
                "check32BitOn64System": bool(r.get("check32")),
                "detectionType": dt if dt in FILE_TYPES else "exists",
                "operator": "notConfigured" if dt in ("exists", "doesNotExist") else (r.get("operator") or "equal"),
                "detectionValue": None if dt in ("exists", "doesNotExist") else str(r.get("value", "")),
            })
        elif t == "registry":
            dt = r.get("detection_type") or "exists"
            out.append({
                "@odata.type": "#microsoft.graph.win32LobAppRegistryDetection",
                "keyPath": r.get("key", "").strip(),
                "valueName": r.get("value_name", "").strip() or None,
                "check32BitOn64System": bool(r.get("check32")),
                "detectionType": dt if dt in REG_TYPES else "exists",
                "operator": "notConfigured" if dt in ("exists", "doesNotExist") else (r.get("operator") or "equal"),
                "detectionValue": None if dt in ("exists", "doesNotExist") else str(r.get("value", "")),
            })
        elif t == "script":
            out.append({
                "@odata.type": "#microsoft.graph.win32LobAppPowerShellScriptDetection",
                "enforceSignatureCheck": bool(r.get("enforce_signature")),
                "runAs32Bit": bool(r.get("run32")),
                "scriptContent": base64.b64encode(r.get("content", "").encode("utf-8")).decode("ascii"),
            })
    return out


def validate(job: dict) -> list[str]:
    errors = []
    rules = job["detection"]["rules"]
    if not rules:
        errors.append("At least one detection rule is required.")
    if any(r.get("type") == "script" for r in rules) and len(rules) > 1:
        errors.append("A script detection rule cannot be combined with other rules.")
    for r in rules:
        if r.get("type") == "msi" and not r.get("product_code"):
            errors.append("MSI detection rule needs a product code.")
        if r.get("type") == "file" and not (r.get("path") and r.get("name")):
            errors.append("File detection rule needs a path and a file/folder name.")
        if r.get("type") == "registry" and not r.get("key"):
            errors.append("Registry detection rule needs a key path.")
        if r.get("type") == "script" and not r.get("content", "").strip():
            errors.append("Script detection rule is empty.")
    if not job["app"].get("name"):
        errors.append("App name is required.")
    if not job["status"].get("intunewin") or not Path(job["status"]["intunewin"]).exists():
        errors.append("No .intunewin file - build the package first.")
    return errors


def display_name(job: dict) -> str:
    a = job["app"]
    if a.get("display_name"):
        return a["display_name"]
    vendor, name = (a.get("vendor") or "").strip(), (a.get("name") or "").strip()
    # 'Mozilla' + 'Mozilla Firefox' -> 'Mozilla Firefox', not 'Mozilla Mozilla Firefox'
    if vendor and name.lower().startswith(vendor.lower()):
        vendor = ""
    return " ".join(x for x in (vendor, name, a.get("version")) if x)


def app_body(job: dict) -> dict:
    a, c, req = job["app"], job["commands"], job["requirements"]
    body = {
        "@odata.type": "#microsoft.graph.win32LobApp",
        "displayName": display_name(job),
        "description": a.get("description") or display_name(job),
        "publisher": a.get("vendor") or "Unknown",
        "developer": a.get("vendor") or "",
        "displayVersion": a.get("version") or "",
        "owner": a.get("owner") or "",
        "notes": a.get("notes") or "Packaged with Pack-A-Lot (PSADT v4)",
        "informationUrl": a.get("information_url") or None,
        "privacyInformationUrl": a.get("privacy_url") or None,
        "isFeatured": bool(a.get("featured")),
        "fileName": Path(job["status"]["intunewin"]).name,
        "setupFilePath": psadt_script.SETUP_FILE,
        "installCommandLine": psadt_script.build_command(c["install"], "Install"),
        "uninstallCommandLine": psadt_script.build_command(c["uninstall"], "Uninstall"),
        "applicableArchitectures": req.get("architectures") or "x64",
        "minimumSupportedOperatingSystem": {req.get("min_os") or "v10_1607": True},
        "detectionRules": detection_rules_body(job["detection"]["rules"]),
        "returnCodes": [{"returnCode": int(rc["code"]), "type": rc["type"]} for rc in job["return_codes"]],
        "installExperience": {
            "runAsAccount": c.get("run_as") or "system",
            "deviceRestartBehavior": c.get("restart_behavior") or "basedOnReturnCode",
            "maxRunTimeInMinutes": int(c.get("max_runtime") or 60),
        },
        "allowAvailableUninstall": bool(c.get("allow_available_uninstall", True)),
    }
    if int(req.get("min_disk_mb") or 0) > 0:
        body["minimumFreeDiskSpaceInMB"] = int(req["min_disk_mb"])
    if int(req.get("min_memory_mb") or 0) > 0:
        body["minimumMemoryInMB"] = int(req["min_memory_mb"])
    icon = _icon_path(job)
    if icon:
        body["largeIcon"] = {"type": "image/png", "value": base64.b64encode(icon.read_bytes()).decode("ascii")}
    return body


def _icon_path(job: dict) -> Path | None:
    custom = job["app"].get("icon_path")
    if custom and Path(custom).is_file():
        return Path(custom)
    default = Path(load_settings()["template_dir"]) / "Assets" / "AppIcon.png"
    return default if default.is_file() else None


def assignments_body(job: dict) -> dict | None:
    items = []
    for a in job.get("assignments") or []:
        target_type = a.get("target")
        if target_type == "allUsers":
            target = {"@odata.type": "#microsoft.graph.allLicensedUsersAssignmentTarget"}
        elif target_type == "allDevices":
            target = {"@odata.type": "#microsoft.graph.allDevicesAssignmentTarget"}
        elif target_type == "group" and a.get("group_id"):
            target = {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": a["group_id"].strip()}
        elif target_type == "exclude" and a.get("group_id"):
            target = {"@odata.type": "#microsoft.graph.exclusionGroupAssignmentTarget", "groupId": a["group_id"].strip()}
        else:
            continue
        items.append({
            "@odata.type": "#microsoft.graph.mobileAppAssignment",
            "intent": a.get("intent") or "required",
            "target": target,
            "settings": {
                "@odata.type": "#microsoft.graph.win32LobAppAssignmentSettings",
                "notifications": a.get("notifications") or "showAll",
                "deliveryOptimizationPriority": "notConfigured",
                "installTimeSettings": None,
                "restartSettings": None,
            },
        })
    return {"mobileAppAssignments": items} if items else None


# --------------------------------------------------------------------------- upload
def run_upload(job: dict, log: LogFn, auth: dict | None = None) -> dict:
    errors = validate(job)
    if errors:
        raise ValueError("\n".join(errors))
    # config.yaml provides the app registration; values passed from the UI override it
    auth = {**load_auth_config(), **{k: v for k, v in (auth or {}).items() if v not in (None, "")}}
    mode = auth.get("auth_mode") or "interactive"
    if mode in ("secret", "certificate") and not (auth.get("tenant_id") and auth.get("client_id")):
        raise ValueError("Tenant ID and Client ID are required for app-only authentication.")

    work = job_dir(job["id"]) / "intune"
    work.mkdir(exist_ok=True)
    app_json = work / "app.json"
    app_json.write_text(json.dumps(app_body(job), indent=2), encoding="utf-8")
    assign = assignments_body(job)
    assign_json = work / "assignments.json"
    if assign:
        assign_json.write_text(json.dumps(assign, indent=2), encoding="utf-8")
    elif assign_json.exists():
        assign_json.unlink()
    config = {
        "intunewin": job["status"]["intunewin"],
        "appJsonPath": str(app_json),
        "assignmentsJsonPath": str(assign_json) if assign else "",
        "cleanupOnFailure": True,
        "auth": {"mode": mode, "tenantId": auth.get("tenant_id", ""), "clientId": auth.get("client_id", ""),
                 "certThumbprint": auth.get("cert_thumbprint", "")},
    }
    config_json = work / "upload-config.json"
    config_json.write_text(json.dumps(config, indent=2), encoding="utf-8")

    env = {}
    if mode == "secret":
        secret = auth.get("client_secret") or os.environ.get("PACKALOT_CLIENT_SECRET", "")
        if not secret:
            raise ValueError("Client secret is required for 'secret' authentication.")
        env["PACKALOT_CLIENT_SECRET"] = secret

    log(f"Uploading '{display_name(job)}' to Intune ({mode} auth)...")
    result: dict = {}

    def capture_log(line: str) -> None:
        nonlocal result
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
        else:
            log(line)

    code = run_process(powershell_cmd(SCRIPTS_DIR / "Upload-Win32App.ps1", "-ConfigPath", str(config_json)),
                       capture_log, env=env, timeout=60 * 120, console=(mode == "interactive"))
    if code != 0 or not result.get("appId"):
        raise RuntimeError(f"Upload failed (exit code {code}). See the log above.")
    url = f"https://intune.microsoft.com/#view/Microsoft_Intune_Apps/SettingsMenu/~/0/appId/{result['appId']}"
    log(f"Intune app id: {result['appId']}")
    job["status"].update({"uploaded": True, "intune_app_id": result["appId"], "intune_url": url})
    return job


def test_connection(log: LogFn, auth: dict | None = None) -> dict:
    """Sign in with the configured app registration and check the Intune / group permissions."""
    auth = {**load_auth_config(), **{k: v for k, v in (auth or {}).items() if v not in (None, "")}}
    mode = auth.get("auth_mode") or "interactive"
    if mode in ("secret", "certificate") and not (auth.get("tenant_id") and auth.get("client_id")):
        raise ValueError("Tenant ID and Client ID are required for app-only authentication (config.yaml).")
    env = {}
    if mode == "secret":
        secret = auth.get("client_secret") or os.environ.get("PACKALOT_CLIENT_SECRET", "")
        if not secret:
            raise ValueError("Enter the client secret to test 'secret' authentication.")
        env["PACKALOT_CLIENT_SECRET"] = secret
    import tempfile
    with tempfile.TemporaryDirectory(prefix="packalot-auth-") as tmp:
        cfg = Path(tmp) / "auth.json"
        cfg.write_text(json.dumps({"auth": {"mode": mode, "tenantId": auth.get("tenant_id", ""),
                                            "clientId": auth.get("client_id", ""),
                                            "certThumbprint": auth.get("cert_thumbprint", "")}}), encoding="utf-8")
        result: dict = {}

        def capture_log(line: str) -> None:
            nonlocal result
            if line.startswith("RESULT:"):
                result = json.loads(line[len("RESULT:"):])
            else:
                log(line)

        run_process(powershell_cmd(SCRIPTS_DIR / "Test-IntuneConnection.ps1", "-ConfigPath", str(cfg)),
                    capture_log, env=env, timeout=60 * 10, console=(mode == "interactive"))
    return result or {"connected": False, "error": "No result from the connection test."}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    job = load_job(argv[0])
    job = run_upload(job, lambda m: print(m, flush=True))
    save_job(job)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
