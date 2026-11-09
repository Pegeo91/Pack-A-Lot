"""Shared helpers: paths, settings, job storage, process execution."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

# When running as Pack-A-Lot.exe (PyInstaller), the editable files (static/, scripts/,
# PSADT Template/, config.yaml) live next to the exe, not in the temporary unpack folder.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
TOOLS_DIR = ROOT / "tools"
STATIC_DIR = ROOT / "static"
SETTINGS_FILE = ROOT / "settings.json"
CONFIG_FILE = ROOT / "config.yaml"

LogFn = Callable[[str], None]

DEFAULT_SETTINGS: dict = {
    "workspace": str(Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "PackALot" / "workspace"),
    "template_dir": str(ROOT / "PSADT Template"),
    "intunewin_util": str(TOOLS_DIR / "IntuneWinAppUtil.exe"),
    "author": "",
    # auto = direct HTTPS download (URL + SHA256 from winget) with 'winget download' as fallback
    "download_method": "auto",  # auto | direct | winget
    # Windows Sandbox
    "sandbox_memory_mb": 4096,
    "sandbox_networking": True,
    "sandbox_timeout_min": 30,
}

# --------------------------------------------------------------------------- Intune app registration (config.yaml)
AUTH_MODES = ("interactive", "devicecode", "secret", "certificate")
DEFAULT_AUTH = {"auth_mode": "interactive", "tenant_id": "", "client_id": "", "cert_thumbprint": ""}

CONFIG_TEMPLATE = """# Pack-A-Lot - Intune upload configuration
# App registration used to upload Win32 apps to Intune with Microsoft Graph.
#
# Required Microsoft Graph permission: DeviceManagementApps.ReadWrite.All
#   - certificate / secret  -> APPLICATION permission + admin consent
#   - interactive / devicecode -> DELEGATED permission + admin consent; the signed-in user also
#     needs an Intune role that can create/assign apps (e.g. Intune Administrator or Application Manager)
#
# The client secret is NEVER stored here - type it in the UI or set the PACKALOT_CLIENT_SECRET
# environment variable. Certificate authentication is recommended.

intune:
  # interactive | devicecode | secret | certificate
  auth_mode: {auth_mode}

  # Directory (tenant) ID - GUID or contoso.onmicrosoft.com
  tenant_id: {tenant_id}

  # Application (client) ID of the app registration
  client_id: {client_id}

  # Certificate thumbprint (auth_mode: certificate). The certificate (with private key)
  # must be in Cert:\\CurrentUser\\My or Cert:\\LocalMachine\\My
  cert_thumbprint: {cert_thumbprint}
"""


def load_auth_config() -> dict:
    """Read the intune section of config.yaml (created with defaults if missing)."""
    if not CONFIG_FILE.exists():
        save_auth_config(DEFAULT_AUTH)
    auth = dict(DEFAULT_AUTH)
    try:
        import yaml
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8-sig")) or {}
        section = data.get("intune") or {}
        for key in DEFAULT_AUTH:
            if section.get(key) is not None:
                auth[key] = str(section[key]).strip()
    except Exception as exc:  # noqa: BLE001 - a broken file must not break the app
        auth["error"] = f"Could not read {CONFIG_FILE.name}: {exc}"
    if auth["auth_mode"] not in AUTH_MODES:
        auth["auth_mode"] = "interactive"
    return auth


def save_auth_config(values: dict) -> dict:
    current = dict(DEFAULT_AUTH)
    if CONFIG_FILE.exists():
        current.update({k: v for k, v in load_auth_config().items() if k in DEFAULT_AUTH})
    for key in DEFAULT_AUTH:
        if key in values and values[key] is not None:
            current[key] = str(values[key]).strip()
    if current["auth_mode"] not in AUTH_MODES:
        current["auth_mode"] = "interactive"
    # json.dumps produces valid YAML double-quoted scalars
    CONFIG_FILE.write_text(CONFIG_TEMPLATE.format(**{k: json.dumps(v) for k, v in current.items()}), encoding="utf-8")
    return current


# --------------------------------------------------------------------------- settings
def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_settings(values: dict) -> dict:
    settings = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in values:
            settings[key] = values[key]
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def workspace() -> Path:
    path = Path(load_settings()["workspace"])
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- jobs
def new_job_config() -> dict:
    """The full wizard state of one packaging job."""
    settings = load_settings()
    return {
        "id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "created": datetime.now().isoformat(timespec="seconds"),
        "step": 1,
        "source": {
            "type": "winget",  # winget | custom
            "winget_id": "",
            "version": "",
            "architecture": "x64",
            "scope": "machine",
            "installer_type": "",
            "locale": "",
            "custom_path": "",
            "extra_files_path": "",
        },
        "installer": {},  # filled by packager.prepare_source
        "app": {
            "vendor": "",
            "name": "",
            "version": "",
            "arch": "x64",
            "lang": "EN",
            "revision": "01",
            "author": settings.get("author", ""),
            "display_name": "",
            "description": "",
            "processes_to_close": "",
            "information_url": "",
            "privacy_url": "",
            "notes": "",
            "owner": "",
            "featured": False,
            "icon_path": "",
        },
        "script": {
            "toggles": {
                "welcome": True,
                "allow_defer": False,
                "defer_times": 3,
                "check_disk_space": True,
                "persist_prompt": True,
                "close_countdown": 60,
                "progress": True,
                "completion_prompt": False,
                "completion_message": "Installation completed successfully.",
                "uninstall_welcome": True,
                "uninstall_progress": True,
            },
            "sections": {
                "pre_install": "",
                "install": "",
                "post_install": "",
                "pre_uninstall": "",
                "uninstall": "",
                "post_uninstall": "",
            },
        },
        "commands": {
            "install": {
                "deploy_mode": "Silent",
                "suppress_reboot": False,
                "terminal_server": False,
                "disable_logging": False,
                "use_custom": False,
                "custom": "",
            },
            "uninstall": {
                "deploy_mode": "Silent",
                "suppress_reboot": False,
                "terminal_server": False,
                "disable_logging": False,
                "use_custom": False,
                "custom": "",
            },
            "run_as": "system",
            "restart_behavior": "basedOnReturnCode",
            "max_runtime": 60,
            "allow_available_uninstall": True,
        },
        "detection": {"rules": []},
        "requirements": {
            "architectures": "x64",
            "min_os": "v10_1607",
            "min_disk_mb": 0,
            "min_memory_mb": 0,
        },
        "return_codes": [
            {"code": 0, "type": "success"},
            {"code": 1707, "type": "success"},
            {"code": 3010, "type": "softReboot"},
            {"code": 1641, "type": "hardReboot"},
            {"code": 1618, "type": "retry"},
        ],
        "test": {"enabled": True, "test_uninstall": True, "deploy_mode": "Silent", "keep_open": False},
        "assignments": [],
        "auto": {},  # last auto-generated defaults (used to avoid overwriting user edits)
        "status": {
            "fetched": False,
            "packaged": False,
            "package_dir": "",
            "intunewin": "",
            "tested": None,
            "test_result": None,
            "uploaded": False,
            "intune_app_id": "",
        },
    }


def job_dir(job_id: str) -> Path:
    if not job_id or any(c in job_id for c in "\\/:.") :
        raise ValueError("Invalid job id")
    return workspace() / job_id


def load_job(job_id: str) -> dict:
    path = job_dir(job_id) / "job.json"
    if not path.exists():
        raise FileNotFoundError(job_id)
    job = json.loads(path.read_text(encoding="utf-8"))
    return _merge_defaults(new_job_config(), job)


def save_job(job: dict) -> None:
    path = job_dir(job["id"])
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(path / "job.json")


def list_jobs() -> list[dict]:
    jobs = []
    for d in sorted(workspace().iterdir(), reverse=True):
        f = d / "job.json"
        if f.exists():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                a = j.get("app", {})
                vendor, name = (a.get("vendor") or "").strip(), (a.get("name") or "").strip()
                if vendor and name.lower().startswith(vendor.lower()):
                    vendor = ""  # 'Mozilla' + 'Mozilla Firefox' -> 'Mozilla Firefox'
                jobs.append({
                    "id": j["id"],
                    "created": j.get("created"),
                    "name": " ".join(x for x in (vendor, name, a.get("version")) if x)
                            or j.get("source", {}).get("winget_id") or "(new)",
                    "step": j.get("step", 1),
                    "status": j.get("status", {}),
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    return jobs


def delete_job(job_id: str) -> None:
    remove_tree(job_dir(job_id))


def remove_tree(path: Path) -> None:
    """rmtree that also handles read-only files/folders (e.g. copied from OneDrive)."""
    import stat

    def on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if not Path(path).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=on_error)


def _merge_defaults(defaults: dict, data: dict) -> dict:
    """Recursively fill keys missing in data (keeps old jobs loadable after upgrades)."""
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
        elif isinstance(value, dict) and isinstance(data[key], dict) and key not in ("installer", "auto"):
            _merge_defaults(value, data[key])
    return data


# --------------------------------------------------------------------------- process helpers
def ps_quote(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def run_process(cmd: list[str], log: LogFn, cwd: str | Path | None = None, env: dict | None = None,
                timeout: int | None = None, console: bool = False) -> int:
    """Run a process, streaming merged stdout/stderr lines to log. Returns the exit code.

    console=True gives the process its own (visible) console window. Needed for interactive
    Microsoft sign-in: the Windows broker (WAM) must attach its dialog to a window.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    flag = "CREATE_NEW_CONSOLE" if console else "CREATE_NO_WINDOW"
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, flag, 0),
    )
    timer = None
    if timeout:
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = _decode(raw).rstrip("\r\n")
            # winget/progress output uses carriage returns; keep only the final state
            line = line.split("\r")[-1].strip()
            if line and not _is_spinner(line):
                log(line)
        return proc.wait()
    finally:
        if timer:
            timer.cancel()


def capture(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run a process and return (exit code, decoded output)."""
    res = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return res.returncode, _decode(res.stdout) + _decode(res.stderr)


def powershell_cmd(script: str | Path, *args: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args]


def run_ps_inline(code: str, timeout: int = 120) -> tuple[int, str]:
    """Run a short inline PowerShell snippet, return (exit code, output)."""
    wrapped = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';" + code
    return capture(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", wrapped], timeout=timeout)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_spinner(line: str) -> bool:
    stripped = line.strip()
    if stripped in ("-", "\\", "|", "/"):
        return True
    # winget progress bars: "████▒▒▒  1.00 MB / 2.00 MB"
    return stripped.startswith(("█", "▒")) and ("MB" in stripped or "KB" in stripped or "%" in stripped)


def safe_name(value: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    return keep.strip("._") or "app"


def timestamp() -> str:
    return time.strftime("%H:%M:%S")
