"""Tests a built PSADT package inside Windows Sandbox.

Flow: write test-config.json + .wsb -> launch Windows Sandbox -> the LogonCommand runs
scripts/Test-Package.ps1 inside the sandbox -> we follow progress.log until result.json appears.

CLI usage:
    python tester.py <job-id>
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

import psadt_script
from common import SCRIPTS_DIR, LogFn, capture, job_dir, load_job, load_settings, remove_tree, save_job

SANDBOX_EXE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
SANDBOX_PKG = r"C:\PackALotPkg"
SANDBOX_TEST = r"C:\PackALotTest"


def sandbox_available() -> bool:
    return SANDBOX_EXE.exists()


def sandbox_running() -> bool:
    _, out = capture(["tasklist", "/FO", "CSV", "/NH"], timeout=30)
    return any(line.lower().startswith('"windowssandbox') for line in out.splitlines())


def build_test_config(job: dict) -> dict:
    t = job["test"]
    install_cmd = dict(job["commands"]["install"])
    uninstall_cmd = dict(job["commands"]["uninstall"])
    # Allow the sandbox run to use a different deploy mode (e.g. Interactive to watch the UI)
    if t.get("deploy_mode") and not install_cmd.get("use_custom"):
        install_cmd["deploy_mode"] = t["deploy_mode"]
    if t.get("deploy_mode") and not uninstall_cmd.get("use_custom"):
        uninstall_cmd["deploy_mode"] = t["deploy_mode"]
    return {
        "install_command": psadt_script.build_command(install_cmd, "Install"),
        "uninstall_command": psadt_script.build_command(uninstall_cmd, "Uninstall"),
        "test_uninstall": bool(t.get("test_uninstall", True)),
        "keep_open": bool(t.get("keep_open", False)),
        "detection_rules": job["detection"]["rules"],
        "return_codes": job["return_codes"],
    }


def write_wsb(path: Path, package_dir: Path, test_dir: Path, settings: dict) -> None:
    command = (f'cmd.exe /c start "Pack-A-Lot test" powershell.exe -NoProfile -ExecutionPolicy Bypass '
               f'-File {SANDBOX_TEST}\\Test-Package.ps1')
    xml = f"""<Configuration>
  <Networking>{"Enable" if settings.get("sandbox_networking", True) else "Disable"}</Networking>
  <MemoryInMB>{int(settings.get("sandbox_memory_mb", 4096))}</MemoryInMB>
  <ClipboardRedirection>Enable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{escape(str(package_dir))}</HostFolder>
      <SandboxFolder>{SANDBOX_PKG}</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{escape(str(test_dir))}</HostFolder>
      <SandboxFolder>{SANDBOX_TEST}</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>{escape(command)}</Command>
  </LogonCommand>
</Configuration>
"""
    path.write_text(xml, encoding="utf-8")


def run_test(job: dict, log: LogFn) -> dict:
    settings = load_settings()
    if not sandbox_available():
        raise RuntimeError("Windows Sandbox is not installed. Enable the 'Windows Sandbox' optional feature "
                           "(Windows Pro/Enterprise) and reboot.")
    package_dir = Path(job["status"].get("package_dir") or "")
    if not job["status"].get("packaged") or not package_dir.is_dir():
        raise RuntimeError("Build the package first.")
    if not job["detection"]["rules"]:
        raise RuntimeError("Add at least one detection rule before testing.")
    if sandbox_running():
        raise RuntimeError("Windows Sandbox is already running (only one instance is allowed). Close it and retry.")

    test_dir = job_dir(job["id"]) / "test"
    if test_dir.exists():
        remove_tree(test_dir)
    results = test_dir / "results"
    results.mkdir(parents=True)
    shutil.copy2(SCRIPTS_DIR / "Test-Package.ps1", test_dir / "Test-Package.ps1")
    config = build_test_config(job)
    (test_dir / "test-config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    wsb = test_dir / "PackALot.wsb"
    write_wsb(wsb, package_dir, test_dir, settings)

    log(f"Install command:   {config['install_command']}")
    log(f"Uninstall command: {config['uninstall_command']}")
    log("Starting Windows Sandbox (first boot can take a minute)...")
    subprocess.Popen([str(SANDBOX_EXE), str(wsb)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

    timeout = int(settings.get("sandbox_timeout_min", 30)) * 60
    started = time.time()
    progress = results / "progress.log"
    result_file = results / "result.json"
    seen = 0
    tick = 0
    last_alive = time.time()
    while True:
        time.sleep(2)
        tick += 1
        if progress.exists():
            lines = progress.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            for line in lines[seen:]:
                log("[sandbox] " + line)
            seen = len(lines)
        if result_file.exists():
            time.sleep(1)  # let the writer finish
            try:
                result = json.loads(result_file.read_text(encoding="utf-8-sig"))
                break
            except json.JSONDecodeError:
                continue
        if time.time() - started > timeout:
            raise TimeoutError(f"Sandbox test did not finish within {timeout // 60} minutes.")
        # Detect the sandbox being closed before the test completed
        if tick % 10 == 0:
            if sandbox_running():
                last_alive = time.time()
            elif time.time() - last_alive > 60 and time.time() - started > 90:
                raise RuntimeError("Windows Sandbox closed before the test finished.")

    result["results_dir"] = str(results)
    if result.get("error"):
        log("Test error: " + result["error"])
    log("RESULT: " + ("SUCCESS" if result.get("success") else "FAILED"))
    job["status"].update({"tested": bool(result.get("success")), "test_result": result})
    return job


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    job = load_job(argv[0])
    job = run_test(job, lambda m: print(m, flush=True))
    save_job(job)
    return 0 if job["status"]["tested"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
