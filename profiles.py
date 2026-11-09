"""Saved per-app customisations ("profiles").

When a package is built, the parts the user added or changed - the custom script sections, the UI toggles,
processes to close, extra files and the output folder - are saved under the app's winget ID (or vendor + name
for own installers). When a new job for the same app downloads its source, the profile is applied
automatically, so a new version gets the same customisations.

Profiles live in %LOCALAPPDATA%\\PackALot\\profiles\\<key>.json.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from common import ROOT, LogFn, safe_name

PROFILES_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "PackALot" / "profiles"
EXTRA_SECTIONS = ("pre_install", "post_install", "pre_uninstall", "post_uninstall")
MAIN_SECTIONS = ("install", "uninstall")


def profile_key(job: dict) -> str:
    src, app = job["source"], job["app"]
    if src.get("type") == "winget" and src.get("winget_id"):
        return safe_name(src["winget_id"].strip().lower())
    name = " ".join(x for x in (app.get("vendor"), app.get("name")) if x).strip()
    return "custom-" + safe_name(name.lower()) if name else ""


def _path(key: str) -> Path:
    if not key or safe_name(key) != key:
        raise ValueError("Invalid profile key")
    return PROFILES_DIR / f"{key}.json"


def load_profile(key: str) -> dict | None:
    try:
        return json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_profiles() -> list[dict]:
    out = []
    if PROFILES_DIR.is_dir():
        for f in sorted(PROFILES_DIR.glob("*.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"key": f.stem, "app": p.get("app", f.stem), "version": p.get("version", ""),
                        "saved": p.get("saved", ""), "items": summary(p)})
    return out


def delete_profile(key: str) -> None:
    _path(key).unlink(missing_ok=True)


def summary(p: dict) -> list[str]:
    """Human-readable list of what a profile contains."""
    labels = {"pre_install": "Pre-Install", "install": "Install", "post_install": "Post-Install",
              "pre_uninstall": "Pre-Uninstall", "uninstall": "Uninstall", "post_uninstall": "Post-Uninstall"}
    items = [labels[k] for k in labels if (p.get("sections") or {}).get(k)]
    if p.get("processes_to_close"):
        items.append("processes to close")
    if p.get("extra_files_path"):
        items.append("extra files")
    if p.get("output_dir"):
        items.append("output folder")
    if p.get("toggles"):
        items.append("dialog settings")
    return items


def save_profile(job: dict, log: LogFn) -> None:
    """Store the job's customisations. Called after a successful build."""
    if not job.get("profile", {}).get("remember", True):
        return
    key = profile_key(job)
    if not key:
        return
    sections = job["script"]["sections"]
    auto = job.get("auto") or {}
    saved_sections = {k: sections[k] for k in EXTRA_SECTIONS if sections.get(k, "").strip()}
    # Install / Uninstall are generated per installer - only keep them when the user changed them
    for k in MAIN_SECTIONS:
        if sections.get(k, "").strip() and sections.get(k) != auto.get(k):
            saved_sections[k] = sections[k]
    a = job["app"]
    vendor, name = (a.get("vendor") or "").strip(), (a.get("name") or "").strip()
    if vendor and name.lower().startswith(vendor.lower()):
        vendor = ""
    profile = {
        "key": key,
        "app": " ".join(x for x in (vendor, name) if x) or key,
        "version": a.get("version", ""),
        "installer_file": (job.get("installer") or {}).get("file", ""),
        "saved": datetime.now().isoformat(timespec="seconds"),
        "sections": saved_sections,
        "toggles": dict(job["script"]["toggles"]),
        "processes_to_close": a.get("processes_to_close", ""),
        "extra_files_path": job["source"].get("extra_files_path", ""),
        "output_dir": job.get("package", {}).get("output_dir", ""),
    }
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _path(key).write_text(json.dumps(profile, indent=2), encoding="utf-8")
    what = ", ".join(summary(profile)) or "dialog settings only"
    log(f"Saved customisations for future versions of this app ({what}).")


def apply_profile(job: dict, log: LogFn) -> None:
    """Re-use a saved profile on a new job. Runs once per job, after the source is downloaded."""
    key = profile_key(job)
    state = job.setdefault("profile", {"remember": True, "applied": None})
    if not key or (state.get("applied") or {}).get("key") == key:
        return
    p = load_profile(key)
    if not p:
        return
    sections = job["script"]["sections"]
    new_file = (job.get("installer") or {}).get("file", "")
    old_file = p.get("installer_file", "")
    for k, code in (p.get("sections") or {}).items():
        if k in MAIN_SECTIONS:
            # e.g. 'Firefox Setup 158.0.exe' -> 'Firefox Setup 159.0.exe' in a customised Install section
            if old_file and new_file and old_file != new_file:
                code = re.sub(re.escape(old_file), lambda _m: new_file, code, flags=re.I)
            sections[k] = code
        elif k in EXTRA_SECTIONS and not sections.get(k, "").strip():
            sections[k] = code
    if p.get("toggles"):
        job["script"]["toggles"].update(p["toggles"])
    for target, field, value in ((job["app"], "processes_to_close", p.get("processes_to_close")),
                                 (job["source"], "extra_files_path", p.get("extra_files_path")),
                                 (job.setdefault("package", {}), "output_dir", p.get("output_dir"))):
        if value and not target.get(field):
            target[field] = value
    state["applied"] = {"key": key, "version": p.get("version", ""), "saved": p.get("saved", ""),
                        "items": summary(p)}
    log(f"Applied saved customisations from version {p.get('version') or '?'} "
        f"({', '.join(summary(p)) or 'dialog settings'}).")
    if any(k in MAIN_SECTIONS for k in p.get("sections") or {}):
        log("NOTE: the Install/Uninstall code was customised for the previous version - review it in the PSADT script step.")
