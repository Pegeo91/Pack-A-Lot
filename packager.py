"""Packaging: fetch installer (winget or custom) -> build PSADT package -> create .intunewin.

CLI usage:
    python packager.py <job-id> fetch     # download / copy the installer and analyse it
    python packager.py <job-id> build     # build the PSADT package + .intunewin
"""
from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

import psadt_script
import winget_client
from common import (SCRIPTS_DIR, LogFn, capture, job_dir, load_job, load_settings, powershell_cmd,
                    ps_quote, remove_tree, run_process, safe_name, save_job)

INTUNEWIN_URL = ("https://raw.githubusercontent.com/microsoft/Microsoft-Win32-Content-Prep-Tool/"
                 "master/IntuneWinAppUtil.exe")


# =========================================================================== step 1: source
def prepare_source(job: dict, log: LogFn) -> dict:
    """Download (winget) or copy (custom) the installer into <job>/source and analyse it."""
    src = job["source"]
    source_dir = job_dir(job["id"]) / "source"
    if source_dir.exists():
        remove_tree(source_dir)
    source_dir.mkdir(parents=True)

    if src["type"] == "winget":
        if not src.get("winget_id"):
            raise ValueError("Select a winget package first.")
        log(f"Downloading {src['winget_id']} {src.get('version') or '(latest)'} via winget...")
        info = winget_client.download(
            src["winget_id"], source_dir, log, version=src.get("version", ""),
            architecture=src.get("architecture", ""), scope=src.get("scope", ""),
            installer_type=src.get("installer_type", ""), locale=src.get("locale", ""),
            method=load_settings().get("download_method", "auto"))
    else:
        path = Path(src.get("custom_path", "").strip().strip('"'))
        if not path.is_file():
            raise FileNotFoundError(f"Installer not found: {path}")
        log(f"Copying custom installer {path}")
        shutil.copy2(path, source_dir / path.name)
        info = {"file": path.name, "winget_type": _sniff_exe_type(source_dir / path.name)}

    installer_path = source_dir / info["file"]
    log(f"Installer: {installer_path.name} ({installer_path.stat().st_size / 1_048_576:.1f} MB)")

    # Read metadata straight from the file (more reliable than the manifest)
    meta = read_installer_metadata(installer_path, log)
    if installer_path.suffix.lower() == ".msi":
        info["product_code"] = meta.get("ProductCode") or info.get("product_code", "")
        info["upgrade_code"] = meta.get("UpgradeCode") or info.get("upgrade_code", "")
        info.setdefault("display_name", "")
        info["display_name"] = info["display_name"] or meta.get("ProductName", "")
        info["display_version"] = info.get("display_version") or meta.get("ProductVersion", "")
    elif installer_path.suffix.lower() == ".exe" and not info.get("winget_type"):
        info["winget_type"] = _sniff_exe_type(installer_path)
    info["metadata"] = meta
    info["kind"] = psadt_script.installer_kind(info)
    if info["kind"] == "exe" and not info.get("winget_type"):
        info["winget_type"] = _sniff_exe_type(installer_path)
    log(f"Detected installer technology: {info.get('winget_type') or info['kind']}")
    job["installer"] = info

    # Pre-fill app details (only fields that are still empty)
    app = job["app"]
    prefill = {
        "vendor": info.get("publisher") or meta.get("Manufacturer", ""),
        "name": info.get("package_name") or meta.get("ProductName", "") or installer_path.stem,
        "version": info.get("version") or meta.get("ProductVersion", "") or meta.get("FileVersion", ""),
        "description": info.get("description", "") or meta.get("FileDescription", ""),
        "information_url": info.get("information_url", ""),
        "privacy_url": info.get("privacy_url", ""),
    }
    for key, value in prefill.items():
        if value and not app.get(key):
            app[key] = str(value).strip()
    arch = (info.get("architecture") or src.get("architecture") or "").lower()
    if arch in ("x64", "x86", "arm64"):
        app["arch"] = arch
        job["requirements"]["architectures"] = "x86,x64" if arch == "x86" else arch

    # Auto-generated defaults: replace only when the user hasn't customised them
    _apply_defaults(job)
    job["status"].update({"fetched": True, "fetched_source": dict(src), "packaged": False, "tested": None,
                          "uploaded": False})
    log("Source ready.")
    return job


def _apply_defaults(job: dict) -> None:
    prev = job.get("auto") or {}
    sections = psadt_script.default_sections(job["installer"], job["app"], job["source"].get("scope", "machine"))
    detection = psadt_script.default_detection(job["installer"], job["app"])
    cur = job["script"]["sections"]
    for key in ("install", "uninstall"):
        if not cur.get(key, "").strip() or cur.get(key) == prev.get(key):
            cur[key] = sections[key]
    if not job["detection"]["rules"] or job["detection"]["rules"] == prev.get("detection"):
        job["detection"]["rules"] = detection
    job["auto"] = {"install": sections["install"], "uninstall": sections["uninstall"], "detection": detection}


def read_installer_metadata(path: Path, log: LogFn) -> dict:
    if path.suffix.lower() not in (".msi", ".exe"):
        return {}
    code, out = capture(powershell_cmd(SCRIPTS_DIR / "Get-InstallerInfo.ps1", "-Path", str(path)), timeout=60)
    try:
        line = next(ln for ln in reversed(out.strip().splitlines()) if ln.strip().startswith("{"))
        return json.loads(line)
    except (StopIteration, json.JSONDecodeError):
        log(f"Could not read installer metadata (exit {code}): {out.strip()[:300]}")
        return {}


def _sniff_exe_type(path: Path) -> str:
    """Guess the installer technology from marker strings in the binary."""
    suffix = path.suffix.lower()
    if suffix == ".msi":
        return "msi"
    if suffix in (".msix", ".msixbundle", ".appx", ".appxbundle"):
        return "msix"
    if suffix == ".zip":
        return "zip"
    try:
        with path.open("rb") as fh:
            data = fh.read(16 * 1024 * 1024)
    except OSError:
        return "exe"
    if b"Nullsoft" in data or b"NSIS" in data:
        return "nullsoft"
    if b"Inno Setup" in data:
        return "inno"
    if b".wixburn" in data or b"WixBundle" in data:
        return "burn"
    return "exe"


def save_custom_upload(job: dict, filename: str, stream) -> Path:
    """Store a browser-uploaded installer in <job>/upload and point the source at it."""
    dest_dir = job_dir(job["id"]) / "upload"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(filename).name
    with dest.open("wb") as fh:
        shutil.copyfileobj(stream, fh, length=1024 * 1024)
    job["source"]["custom_path"] = str(dest)
    return dest


# =========================================================================== step 5: build
def build_package(job: dict, log: LogFn) -> dict:
    settings = load_settings()
    template = Path(settings["template_dir"])
    if not (template / "Invoke-AppDeployToolkit.ps1").exists():
        raise FileNotFoundError(f"PSADT template not found at {template}")
    installer = job.get("installer") or {}
    source_dir = job_dir(job["id"]) / "source"
    if not installer.get("file") or not (source_dir / installer["file"]).exists():
        raise RuntimeError("No installer available - complete the Source step first.")

    base = job_dir(job["id"])
    pkg = base / "package"
    out = base / "output"
    for d in (pkg, out):
        if d.exists():
            remove_tree(d)
    out.mkdir(parents=True)

    log(f"Copying PSADT template from {template}")
    shutil.copytree(template, pkg, ignore=shutil.ignore_patterns("Add Setup Files Here.txt"))
    files_dir = pkg / "Files"
    files_dir.mkdir(exist_ok=True)

    log(f"Adding installer {installer['file']} to Files\\")
    shutil.copy2(source_dir / installer["file"], files_dir / installer["file"])

    extra = job["source"].get("extra_files_path", "").strip().strip('"')
    if extra:
        extra_path = Path(extra)
        if extra_path.is_dir():
            log(f"Adding extra files from {extra_path} to Files\\")
            shutil.copytree(extra_path, files_dir, dirs_exist_ok=True)
        elif extra_path.is_file():
            shutil.copy2(extra_path, files_dir / extra_path.name)
        else:
            log(f"WARNING: extra files path not found, skipped: {extra}")

    icon = job["app"].get("icon_path")
    if icon and Path(icon).is_file():
        shutil.copy2(icon, pkg / "Assets" / "AppIcon.png")

    log("Generating Invoke-AppDeployToolkit.ps1")
    template_text = (template / "Invoke-AppDeployToolkit.ps1").read_text(encoding="utf-8-sig")
    script = psadt_script.generate(template_text, job)
    (pkg / "Invoke-AppDeployToolkit.ps1").write_text(script, encoding="utf-8-sig")
    _syntax_check(pkg / "Invoke-AppDeployToolkit.ps1", log)

    tool = ensure_intunewin_util(log)
    log("Creating .intunewin package...")
    code = run_process([str(tool), "-c", str(pkg), "-s", psadt_script.SETUP_FILE, "-o", str(out), "-q"],
                       log, timeout=60 * 60)
    produced = out / (Path(psadt_script.SETUP_FILE).stem + ".intunewin")
    if code != 0 or not produced.exists():
        raise RuntimeError(f"IntuneWinAppUtil failed (exit code {code})")
    a = job["app"]
    final = out / (safe_name("_".join(x for x in (a.get("vendor"), a.get("name"), a.get("version")) if x)) + ".intunewin")
    produced.replace(final)
    log(f"Created {final} ({final.stat().st_size / 1_048_576:.1f} MB)")

    job["status"].update({"packaged": True, "package_dir": str(pkg), "intunewin": str(final),
                          "tested": None, "test_result": None, "uploaded": False})
    return job


def _syntax_check(script: Path, log: LogFn) -> None:
    code = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"{ps_quote(str(script))},[ref]$null,[ref]$e);if($e){{$e|%{{\"Line $($_.Extent.StartLineNumber): $($_.Message)\"}};exit 1}}")
    rc, out = capture(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code], timeout=60)
    if rc != 0:
        raise RuntimeError("Generated script has PowerShell syntax errors:\n" + out.strip())
    log("Script syntax check passed.")


def ensure_intunewin_util(log: LogFn) -> Path:
    tool = Path(load_settings()["intunewin_util"])
    if tool.exists():
        return tool
    log(f"IntuneWinAppUtil.exe not found - downloading from {INTUNEWIN_URL}")
    tool.parent.mkdir(parents=True, exist_ok=True)
    tmp = tool.with_suffix(".download")
    with urllib.request.urlopen(INTUNEWIN_URL, timeout=120) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(tool)
    log("IntuneWinAppUtil.exe downloaded.")
    return tool


def preview_script(job: dict) -> str:
    template = Path(load_settings()["template_dir"]) / "Invoke-AppDeployToolkit.ps1"
    return psadt_script.generate(template.read_text(encoding="utf-8-sig"), job)


# =========================================================================== CLI
def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("fetch", "build"):
        print(__doc__)
        return 2
    job = load_job(argv[0])
    log = lambda m: print(m, flush=True)  # noqa: E731
    job = prepare_source(job, log) if argv[1] == "fetch" else build_package(job, log)
    save_job(job)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
