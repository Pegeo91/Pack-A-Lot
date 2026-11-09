"""Pack-A-Lot - browser-based wizard: winget/custom installer -> PSADT -> Sandbox test -> Intune.

Run:  python app.py   (opens http://127.0.0.1:5050)
"""
from __future__ import annotations

import json
import os
import threading
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

import packager
import profiles
import psadt_script
import tester
import uploader
import winget_client
from common import (CONFIG_FILE, ROOT, SCRIPTS_DIR, STATIC_DIR, capture, delete_job, job_dir, list_jobs,
                    load_auth_config, load_job, load_settings, new_job_config, powershell_cmd, rename_job,
                    save_auth_config, save_job, save_settings, timestamp)

HOST, PORT = "127.0.0.1", int(os.environ.get("PACKALOT_PORT", "5050"))
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024  # installers can be big


# --------------------------------------------------------------------------- background tasks
class Task:
    def __init__(self, name: str):
        self.name = name
        self.state = "running"  # running | success | failed
        self.log: list[str] = []
        self.error = ""
        self.lock = threading.Lock()

    def write(self, message: str) -> None:
        with self.lock:
            for line in str(message).splitlines() or [""]:
                self.log.append(f"[{timestamp()}] {line}")


TASKS: dict[str, Task] = {}
TASK_FUNCS = {
    "fetch": packager.prepare_source,
    "build": packager.build_package,
    "export": packager.export_package,
    "test": tester.run_test,
    "upload": None,  # needs extra auth args, see start_task
}


def _run(job_id: str, task: Task, func, kwargs: dict) -> None:
    try:
        job = load_job(job_id)
        job = func(job, task.write, **kwargs)
        save_job(job)
        task.state = "success"
        task.write(f"{task.name} completed.")
    except Exception as exc:  # noqa: BLE001 - surface every failure in the UI
        task.error = str(exc)
        task.write(f"ERROR: {exc}")
        if not isinstance(exc, (ValueError, RuntimeError, FileNotFoundError, TimeoutError)):
            task.write(traceback.format_exc())
        task.state = "failed"
    finally:
        log_file = job_dir(job_id) / f"{task.name}.log"
        try:
            log_file.write_text("\n".join(task.log), encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------- security: local only
@app.before_request
def same_origin_only():
    origin = request.headers.get("Origin")
    if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
        abort(403)


# --------------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# --------------------------------------------------------------------------- environment
@app.get("/api/prereqs")
def prereqs():
    s = load_settings()
    template = Path(s["template_dir"])
    return jsonify({
        "winget": winget_client.available(),
        "sandbox": tester.sandbox_available(),
        "intunewin_util": Path(s["intunewin_util"]).exists(),
        "graph_module": uploader.graph_module_installed(),
        "template": (template / "Invoke-AppDeployToolkit.ps1").exists(),
        "template_dir": str(template),
        "workspace": s["workspace"],
    })


# --------------------------------------------------------------------------- system check (new PC setup)
CHECK_SCRIPT = SCRIPTS_DIR / "Check-Prerequisites.ps1"
FIXABLE = {"sandbox", "winget", "graph_module", "intunewin"}


def _ps_json(args: list[str], timeout: int) -> object:
    code, out = capture(powershell_cmd(CHECK_SCRIPT, *args), timeout=timeout)
    line = next((ln for ln in reversed(out.strip().splitlines()) if ln.strip()[:1] in "[{"), "")
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        raise RuntimeError(f"System check failed (exit {code}): {out.strip()[-500:]}") from None


@app.get("/api/system-check")
def system_check():
    try:
        return jsonify(_ps_json(["-Json", "-Root", str(ROOT)], timeout=180))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/system-fix")
def system_fix():
    fix_id = (request.get_json(force=True) or {}).get("id", "")
    if fix_id not in FIXABLE:
        return jsonify({"error": f"No automatic fix for '{fix_id}'."}), 400
    try:
        res = _ps_json(["-Json", "-Fix", fix_id, "-Root", str(ROOT)], timeout=60 * 15)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    res = (res or [{}])[0] if isinstance(res, list) else res
    return jsonify(res), (200 if res.get("ok") else 500)


INTUNE_TEST = {"running": False, "log": [], "result": None}


@app.post("/api/intune-test")
def intune_test():
    if INTUNE_TEST["running"]:
        return jsonify({"error": "A connection test is already running."}), 409
    body = request.get_json(force=True) or {}
    INTUNE_TEST.update(running=True, log=[], result=None)

    def work():
        try:
            INTUNE_TEST["result"] = uploader.test_connection(lambda m: INTUNE_TEST["log"].append(m), body)
        except Exception as exc:  # noqa: BLE001
            INTUNE_TEST["result"] = {"connected": False, "error": str(exc)}
        finally:
            INTUNE_TEST["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/intune-test")
def intune_test_status():
    return jsonify(INTUNE_TEST)


@app.get("/api/settings")
def get_settings():
    return jsonify(load_settings())


@app.put("/api/settings")
def put_settings():
    return jsonify(save_settings(request.get_json(force=True) or {}))


@app.get("/api/auth-config")
def get_auth_config():
    return jsonify({**load_auth_config(), "path": str(CONFIG_FILE)})


@app.put("/api/auth-config")
def put_auth_config():
    data = request.get_json(force=True) or {}
    data.pop("client_secret", None)  # never persisted
    return jsonify({**save_auth_config(data), "path": str(CONFIG_FILE)})


# --------------------------------------------------------------------------- winget
@app.get("/api/winget/search")
def winget_search():
    return jsonify(winget_client.search(request.args.get("q", "")))


@app.get("/api/winget/versions")
def winget_versions():
    return jsonify(winget_client.versions(request.args.get("id", "")))


@app.get("/api/winget/installer")
def winget_installer():
    a = request.args
    if not a.get("id"):
        return jsonify({"error": "id is required"}), 400
    return jsonify(winget_client.installer_preview(
        a["id"], version=a.get("version", ""), architecture=a.get("architecture", ""), scope=a.get("scope", ""),
        installer_type=a.get("installer_type", ""), locale=a.get("locale", "")))


# --------------------------------------------------------------------------- jobs
@app.get("/api/jobs")
def jobs_list():
    return jsonify(list_jobs())


@app.post("/api/jobs")
def jobs_create():
    job = new_job_config()
    save_job(job)
    return jsonify(job)


@app.get("/api/jobs/<job_id>")
def jobs_get(job_id):
    try:
        return jsonify(load_job(job_id))
    except (FileNotFoundError, ValueError):
        abort(404)


@app.put("/api/jobs/<job_id>")
def jobs_put(job_id):
    current = load_job(job_id)
    data = request.get_json(force=True) or {}
    # The UI may not change server-owned fields
    for key in ("id", "created", "status", "installer", "auto"):
        data[key] = current[key]
    _invalidate(current, data)
    save_job(data)
    return jsonify(load_job(job_id))


PACKAGE_APP_KEYS = ("vendor", "name", "version", "arch", "lang", "revision", "author", "processes_to_close", "icon_path")


def _invalidate(old: dict, new: dict) -> None:
    """Mark the build / test as stale when settings they depend on change."""
    status = new["status"]
    pkg_changed = (old["source"] != new.get("source") or old["script"] != new.get("script")
                   or any(old["app"].get(k) != new.get("app", {}).get(k) for k in PACKAGE_APP_KEYS))
    test_changed = (old["detection"] != new.get("detection") or old["return_codes"] != new.get("return_codes")
                    or old["commands"]["install"] != new.get("commands", {}).get("install")
                    or old["commands"]["uninstall"] != new.get("commands", {}).get("uninstall"))
    if pkg_changed and status.get("packaged"):
        status["packaged"] = False
    if (pkg_changed or test_changed) and status.get("tested") is not None:
        status["tested"] = None
    if pkg_changed or test_changed:
        status["uploaded"] = False


@app.post("/api/jobs/<job_id>/rename")
def jobs_rename(job_id):
    if job_id in TASKS and TASKS[job_id].state == "running":
        return jsonify({"error": "Wait for the running task to finish before renaming."}), 409
    name = (request.get_json(force=True) or {}).get("name", "")
    try:
        job = rename_job(job_id, name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if job_id in TASKS and job["id"] != job_id:
        TASKS[job["id"]] = TASKS.pop(job_id)  # keep the last task log visible
    return jsonify(job)


@app.delete("/api/jobs/<job_id>")
def jobs_delete(job_id):
    if job_id in TASKS and TASKS[job_id].state == "running":
        return jsonify({"error": "A task is running for this job."}), 409
    delete_job(job_id)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/installer-file")
def upload_installer(job_id):
    job = load_job(job_id)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400
    dest = packager.save_custom_upload(job, f.filename, f.stream)
    save_job(job)
    return jsonify({"path": str(dest)})


@app.post("/api/jobs/<job_id>/icon")
def upload_icon(job_id):
    job = load_job(job_id)
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".png"):
        return jsonify({"error": "Please upload a PNG file"}), 400
    dest = job_dir(job_id) / "icon.png"
    f.save(dest)
    job["app"]["icon_path"] = str(dest)
    save_job(job)
    return jsonify({"path": str(dest)})


@app.get("/api/jobs/<job_id>/icon")
def get_icon(job_id):
    job = load_job(job_id)
    path = Path(job["app"].get("icon_path") or Path(load_settings()["template_dir"]) / "Assets" / "AppIcon.png")
    if not path.is_file():
        abort(404)
    return send_from_directory(path.parent, path.name)


@app.post("/api/jobs/<job_id>/preview")
def preview(job_id):
    job = request.get_json(force=True) or load_job(job_id)
    try:
        return jsonify({
            "script": packager.preview_script(job),
            "install_command": psadt_script.build_command(job["commands"]["install"], "Install"),
            "uninstall_command": psadt_script.build_command(job["commands"]["uninstall"], "Uninstall"),
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.post("/api/jobs/<job_id>/commands")
def commands_preview(job_id):
    job = request.get_json(force=True)
    return jsonify({
        "install_command": psadt_script.build_command(job["commands"]["install"], "Install"),
        "uninstall_command": psadt_script.build_command(job["commands"]["uninstall"], "Uninstall"),
    })


@app.post("/api/jobs/<job_id>/detection-script")
def detection_script(job_id):
    data = request.get_json(force=True) or {}
    return jsonify({"script": psadt_script.detection_script(data.get("name", ""), data.get("version", ""))})


@app.post("/api/jobs/<job_id>/uninstall-code")
def uninstall_code(job_id):
    """Uninstall code for an app registered during the sandbox test (uses its real DisplayName)."""
    job = load_job(job_id)
    data = request.get_json(force=True) or {}
    key = data.get("key_name", "")
    if data.get("windows_installer") and key.startswith("{") and key.endswith("}"):
        code = f"Start-ADTMsiProcess -Action Uninstall -ProductCode {psadt_script.ps_quote(key)}"
    else:
        kind = "msi" if data.get("windows_installer") else "exe"
        code = psadt_script.uninstall_by_name(data.get("name", ""), kind, job["installer"].get("winget_type", "exe"))
    return jsonify({"code": code})


@app.post("/api/jobs/<job_id>/validate")
def validate(job_id):
    return jsonify({"errors": uploader.validate(load_job(job_id))})


@app.post("/api/jobs/<job_id>/open")
def open_folder(job_id):
    what = (request.get_json(silent=True) or {}).get("what", "job")
    job = load_job(job_id)
    paths = {
        "job": job_dir(job_id),
        "package": Path(job["status"].get("package_dir") or job_dir(job_id)),
        "output": Path(job["status"].get("intunewin") or job_dir(job_id)).parent,
        "results": job_dir(job_id) / "test" / "results",
        "export": Path(job["status"].get("export_dir") or job.get("package", {}).get("output_dir") or job_dir(job_id)),
    }
    path = paths.get(what, job_dir(job_id))
    if path.exists():
        os.startfile(str(path))  # noqa: S606 - local desktop app
    return jsonify({"path": str(path)})


# --------------------------------------------------------------------------- saved app profiles
@app.get("/api/profiles")
def profiles_list():
    return jsonify(profiles.list_profiles())


@app.get("/api/jobs/<job_id>/profile")
def job_profile(job_id):
    job = load_job(job_id)
    key = profiles.profile_key(job)
    p = profiles.load_profile(key) if key else None
    return jsonify({"key": key, "exists": bool(p), "version": (p or {}).get("version", ""),
                    "saved": (p or {}).get("saved", ""), "items": profiles.summary(p) if p else []})


@app.delete("/api/profiles/<key>")
def profiles_delete(key):
    profiles.delete_profile(key)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- tasks
@app.post("/api/jobs/<job_id>/tasks/<name>")
def start_task(job_id, name):
    if name not in TASK_FUNCS:
        abort(404)
    load_job(job_id)  # 404 early if missing
    existing = TASKS.get(job_id)
    if existing and existing.state == "running":
        return jsonify({"error": f"'{existing.name}' is already running."}), 409
    kwargs = {}
    func = TASK_FUNCS[name]
    if name == "upload":
        func = uploader.run_upload
        kwargs = {"auth": request.get_json(silent=True) or {}}
    task = Task(name)
    TASKS[job_id] = task
    threading.Thread(target=_run, args=(job_id, task, func, kwargs), daemon=True).start()
    return jsonify({"started": name})


@app.get("/api/jobs/<job_id>/live-log")
def live_log(job_id):
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    return jsonify(tester.read_live_log(job_id, offset))


@app.get("/api/jobs/<job_id>/task")
def task_status(job_id):
    task = TASKS.get(job_id)
    if not task:
        return jsonify({"name": None, "state": "idle", "log": [], "offset": 0})
    offset = int(request.args.get("offset", 0))
    with task.lock:
        lines = task.log[offset:]
        total = len(task.log)
    return jsonify({"name": task.name, "state": task.state, "error": task.error, "log": lines, "offset": total})


def _already_running() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    open_browser = not os.environ.get("PACKALOT_NO_BROWSER")
    if _already_running():
        print(f"Pack-A-Lot is already running - opening {url}")
        if open_browser:
            webbrowser.open(url)
        return
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("Pack-A-Lot - close this window to stop")
    load_auth_config()  # creates config.yaml on first start
    print(f"Pack-A-Lot running at {url}")
    print(f"Intune app registration: {CONFIG_FILE}")
    print("Close this window (or press Ctrl+C) to stop.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
