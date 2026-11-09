"""Thin wrapper around the winget CLI: search, list versions, download + manifest parsing."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None

from common import LogFn, capture, run_process

COMMON_ARGS = ["--accept-source-agreements", "--disable-interactivity"]
INSTALLER_EXTENSIONS = {".msi", ".exe", ".msix", ".msixbundle", ".appx", ".appxbundle", ".zip"}


def available() -> bool:
    return shutil.which("winget") is not None


def search(query: str, limit: int = 40) -> list[dict]:
    """Search the winget source. Returns [{name, id, version, match}]."""
    if not query.strip():
        return []
    _, out = capture(["winget", "search", query, "--source", "winget", "--count", str(limit), *COMMON_ARGS], timeout=90)
    return _parse_table(out)


def versions(package_id: str) -> list[str]:
    code, out = capture(["winget", "show", "--id", package_id, "--exact", "--source", "winget", "--versions",
                         *COMMON_ARGS], timeout=90)
    lines = [ln.split("\r")[-1].strip() for ln in out.splitlines()]
    try:
        start = next(i for i, ln in enumerate(lines) if ln and set(ln) == {"-"})
    except StopIteration:
        return []
    return [ln for ln in lines[start + 1:] if ln]


def _selector_args(version: str, architecture: str, scope: str, installer_type: str, locale: str) -> list[str]:
    args = []
    for flag, value in (("--version", version), ("--architecture", architecture), ("--scope", scope),
                        ("--installer-type", installer_type), ("--locale", locale)):
        if value:
            args += [flag, value]
    return args


def download(package_id: str, dest: Path, log: LogFn, version: str = "", architecture: str = "",
             scope: str = "", installer_type: str = "", locale: str = "", method: str = "auto") -> dict:
    """Download the installer to dest. Returns installer info dict.

    method: auto   - direct HTTPS download (URL + SHA256 from 'winget show'), fall back to 'winget download'
            direct - direct download only
            winget - 'winget download' only
    """
    dest.mkdir(parents=True, exist_ok=True)
    selectors = _selector_args(version, architecture, scope, installer_type, locale)
    errors = []

    if method in ("auto", "direct"):
        try:
            return _direct_download(package_id, dest, log, selectors, architecture)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"direct download: {exc}")
            log(f"Direct download failed: {exc}")
            if method == "direct" or "no installer matches" in str(exc):
                raise RuntimeError(_failure_hint(errors)) from exc  # winget download would fail the same way
            log("Falling back to 'winget download'...")
            _clean(dest)

    try:
        return _winget_download(package_id, dest, log, selectors, architecture)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"winget download: {exc}")
        raise RuntimeError(_failure_hint(errors)) from exc


def _clean(dest: Path) -> None:
    for f in dest.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)


def _failure_hint(errors: list[str]) -> str:
    text = " | ".join(errors)
    hint = "Download failed: " + text
    if re.search(r"timed out|0x80072ee2|timeout|unreachable|getaddrinfo|10060|11001", text, re.I):
        hint += ("\nThe installer host could not be reached (network timeout). Check firewall / proxy / web filter "
                 "rules for the host shown above (GitHub releases redirect to release-assets.githubusercontent.com), "
                 "or download the file in a browser and use 'Own installer'.")
    else:
        hint += "\nTry a different architecture / scope / installer type, or leave them empty."
    if "github" in text.lower():
        hint += _github_status()
    return hint


def _github_status() -> str:
    """Report active GitHub incidents (GitHub hosts most winget installers)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("https://www.githubstatus.com/api/v2/summary.json", timeout=8) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001 - best effort only
        return ""
    incidents = [i for i in data.get("incidents", []) if i.get("status") != "resolved"]
    if not incidents:
        return "\nGitHub status: all systems operational - the problem is likely on the network path to GitHub."
    lines = "; ".join(f"{i['name']} ({i['status']}, since {i.get('started_at', '')[:16].replace('T', ' ')} UTC)"
                      for i in incidents)
    return (f"\nGitHub is currently reporting an incident: {lines}. Wait until it is resolved "
            "(https://www.githubstatus.com), or download the installer in a browser where you are signed in "
            "to GitHub and use 'Own installer'.")


# --------------------------------------------------------------------------- winget download
def _winget_download(package_id: str, dest: Path, log: LogFn, selectors: list[str], architecture: str) -> dict:
    cmd = ["winget", "download", "--id", package_id, "--exact", "--source", "winget",
           "--download-directory", str(dest), "--accept-package-agreements", *COMMON_ARGS, *selectors]
    log("> " + " ".join(cmd))
    code = run_process(cmd, log, timeout=60 * 30)
    if code != 0:
        raise RuntimeError(f"exit code 0x{code & 0xFFFFFFFF:08X}")

    files = [f for f in dest.iterdir() if f.is_file()]
    installers = [f for f in files if f.suffix.lower() in INSTALLER_EXTENSIONS]
    manifests = [f for f in files if f.suffix.lower() in (".yaml", ".yml")]
    if not installers:
        raise RuntimeError("winget reported success but no installer file was found in " + str(dest))
    installer_file = max(installers, key=lambda f: f.stat().st_mtime)
    manifest = _load_yaml(manifests[0]) if manifests else {}
    return describe_from_manifest(manifest, installer_file, architecture)


# --------------------------------------------------------------------------- direct download
TYPE_EXTENSIONS = {"msi": ".msi", "wix": ".msi", "msix": ".msix", "appx": ".appx", "zip": ".zip"}


def show(package_id: str, selectors: list[str] | None = None) -> dict:
    """Resolve package metadata + the selected installer's URL / SHA256 via 'winget show'."""
    code, out = capture(["winget", "show", "--id", package_id, "--exact", "--source", "winget",
                         *COMMON_ARGS, *(selectors or [])], timeout=120)
    info = parse_show(out)
    info["exit_code"] = code
    info["raw"] = out
    return info


def parse_show(out: str) -> dict:
    lines = [ln.split("\r")[-1].rstrip() for ln in out.splitlines()]
    info: dict = {}
    # "Found Notepad++ [Notepad++.Notepad++]" (first word is localised)
    for ln in lines:
        m = re.match(r"^\S+\s+(.+?)\s+\[([^\]]+)\]\s*$", ln)
        if m:
            info["name"], info["id"] = m.group(1), m.group(2)
            break
    # English top-level fields (best effort; other UI languages just skip these)
    keys = {"Version": "version", "Publisher": "publisher", "Description": "description",
            "Homepage": "homepage", "Privacy Url": "privacy_url"}
    for ln in lines:
        m = re.match(r"^([A-Za-z][A-Za-z ]+):\s+(.+)$", ln)
        if m and m.group(1) in keys and keys[m.group(1)] not in info:
            info[keys[m.group(1)]] = m.group(2).strip()
    # Installer block (language independent): the SHA256 line, with the URL / type lines just above it
    sha_idx = next((i for i, ln in enumerate(lines) if re.search(r"\b[0-9a-fA-F]{64}\b", ln)), None)
    if sha_idx is not None:
        info["sha256"] = re.search(r"\b([0-9a-fA-F]{64})\b", lines[sha_idx]).group(1)
        block_start = sha_idx
        while block_start > 0 and lines[block_start - 1].startswith((" ", "\t")):
            block_start -= 1
        block = lines[block_start:sha_idx + 1] + lines[sha_idx + 1:sha_idx + 6]
    else:
        # No hash: only look inside the last section ("Installer:"), never elsewhere in the output
        last_header = max((i for i, ln in enumerate(lines) if ln.strip() and not ln.startswith((" ", "\t"))), default=-1)
        block = lines[last_header + 1:]
        info["installer_message"] = " ".join(ln.strip() for ln in block if ln.strip())
    url = next((m.group(0) for ln in block if ln.startswith((" ", "\t")) for m in [re.search(r"https?://\S+", ln)] if m), "")
    info["url"] = url
    known = {"msi", "wix", "exe", "nullsoft", "inno", "burn", "msix", "appx", "zip", "portable"}
    for ln in block:
        value = ln.split(":", 1)[1].strip().lower() if ":" in ln else ""
        if value in known:
            info["installer_type"] = value
            break
    return info


def url_file_name(url: str) -> str:
    import urllib.parse
    return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name if url else ""


# Words in installer file names that usually reveal the install scope
_USER_HINTS = r"user[-_ ]?setup|per[-_ ]?user|current[-_ ]?user|[-_.]user[-_.]|^user|appdata"
_MACHINE_HINTS = r"system[-_ ]?setup|per[-_ ]?machine|all[-_ ]?users|machine|enterprise|[-_.]system[-_.]"


def scope_hint(file_name: str, installer_type: str = "") -> dict:
    """Guess user / machine scope from the binary's name. Returns {scope, reason} (scope '' = unknown)."""
    n = (file_name or "").lower()
    if re.search(_USER_HINTS, n):
        return {"scope": "user", "reason": f"'{file_name}' contains a per-user marker (e.g. 'UserSetup' / 'user') - "
                                            "it most likely installs into the user profile (AppData), not Program Files."}
    if re.search(_MACHINE_HINTS, n):
        return {"scope": "machine", "reason": f"'{file_name}' contains a machine-wide marker (e.g. 'SystemSetup' / "
                                               "'machine' / 'enterprise') - it most likely installs for all users."}
    if n.endswith(".msi") or installer_type in ("msi", "wix"):
        return {"scope": "machine", "reason": "MSI installers normally install per-machine when run as SYSTEM "
                                               "(unless the package is built per-user)."}
    return {"scope": "", "reason": "The file name gives no scope hint."}


def installer_preview(package_id: str, version: str = "", architecture: str = "", scope: str = "",
                      installer_type: str = "", locale: str = "") -> dict:
    """What would be downloaded with these filters - and, if nothing matches, what is available without them."""
    def one(arch, sc, itype):
        info = show(package_id, _selector_args(version, arch, sc, itype, locale))
        name = url_file_name(info.get("url", ""))
        return {"matched": bool(info.get("url")), "url": info.get("url", ""), "file_name": name,
                "installer_type": info.get("installer_type", ""), "version": info.get("version", ""),
                "message": info.get("installer_message", ""),
                "hint": scope_hint(name, info.get("installer_type", "")) if info.get("url") else None}

    result = one(architecture, scope, installer_type)
    if not result["matched"] and (architecture or scope or installer_type):
        result["fallback"] = one("", "", "")
    return result


def _direct_download(package_id: str, dest: Path, log: LogFn, selectors: list[str], architecture: str) -> dict:
    log("Resolving installer with 'winget show'...")
    info = show(package_id, selectors)
    if not info.get("url"):
        detail = info.get("installer_message") or " ".join(info.get("raw", "").split())[-300:]
        raise RuntimeError(f"no installer matches the selected architecture / scope / installer type ({detail}). "
                           "Try leaving those fields empty (Any).")
    log(f"Found {info.get('name', package_id)} {info.get('version', '')} - installer type: "
        f"{info.get('installer_type') or 'unknown'}")
    path = http_download(info["url"], dest, log, info.get("sha256", ""), info.get("installer_type", ""))
    manifest = {
        "PackageIdentifier": package_id,
        "PackageName": info.get("name", ""),
        "PackageVersion": info.get("version", ""),
        "Publisher": info.get("publisher", ""),
        "ShortDescription": info.get("description", ""),
        "PackageUrl": info.get("homepage", ""),
        "PrivacyUrl": info.get("privacy_url", ""),
        "InstallerType": info.get("installer_type", ""),
        "Architecture": architecture,
    }
    return describe_from_manifest(manifest, path, architecture)


def http_download(url: str, dest: Path, log: LogFn, sha256: str = "", installer_type: str = "",
                  retries: int = 3) -> Path:
    """Stream a file to dest (system proxy honoured), verify SHA256. Returns the file path."""
    import hashlib
    import time
    import urllib.parse
    import urllib.request

    host = urllib.parse.urlparse(url).netloc
    log(f"Downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Pack-A-Lot/1.0"})
    for attempt in range(1, retries + 1):
        path = None
        try:
            with urllib.request.urlopen(request, timeout=45) as resp:
                final_host = urllib.parse.urlparse(resp.geturl()).netloc
                if final_host != host:
                    log(f"Redirected to {final_host}")
                path = dest / _download_name(url, resp, installer_type)
                total = int(resp.headers.get("Content-Length") or 0)
                digest = hashlib.sha256()
                done, next_mark, started = 0, 10, time.time()
                with path.open("wb") as fh:
                    while chunk := resp.read(1024 * 1024):
                        fh.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        pct = done * 100 // total if total else 0
                        if (total and pct >= next_mark) or (not total and done >= next_mark * 1048576):
                            speed = done / 1048576 / max(time.time() - started, 0.1)
                            log(f"  {pct}%  {done / 1048576:.1f} / {total / 1048576:.1f} MB  ({speed:.1f} MB/s)"
                                if total else f"  {done / 1048576:.1f} MB")
                            next_mark += 10
            break
        except Exception as exc:  # noqa: BLE001
            if path is not None and path.exists():
                path.unlink()
            log(f"Attempt {attempt}/{retries} failed ({host}): {exc}")
            if attempt == retries:
                raise RuntimeError(f"{host}: {exc}") from exc
            time.sleep(3 * attempt)

    if sha256:
        actual = digest.hexdigest()
        if actual.lower() != sha256.lower():
            path.unlink()
            raise RuntimeError(f"SHA256 mismatch (expected {sha256}, got {actual}) - file discarded.")
        log("SHA256 verified.")
    else:
        log("WARNING: no SHA256 in the manifest - hash not verified.")
    log(f"Saved {path.name} ({done / 1048576:.1f} MB)")
    return path


def _download_name(url: str, resp, installer_type: str) -> str:
    import urllib.parse
    candidates = [Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name]
    disposition = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    if m:
        candidates.append(urllib.parse.unquote(m.group(1)))
    candidates.append(Path(urllib.parse.unquote(urllib.parse.urlparse(resp.geturl()).path)).name)
    clean = [re.sub(r"[^\w.\-+ ()]", "_", c).strip() for c in candidates if c]
    name = next((c for c in clean if Path(c).suffix.lower() in INSTALLER_EXTENSIONS), clean[0] if clean else "installer")
    if Path(name).suffix.lower() not in INSTALLER_EXTENSIONS:
        name += TYPE_EXTENSIONS.get(installer_type, ".exe")
    return name


# --------------------------------------------------------------------------- manifest parsing
def describe_from_manifest(manifest: dict, installer_file: Path, architecture: str = "") -> dict:
    """Combine top-level and per-installer manifest fields into a flat description."""
    installers = manifest.get("Installers") or []
    chosen = _pick_installer(installers, installer_file, architecture) if installers else {}

    def field(name, default=None):
        if chosen.get(name) not in (None, "", []):
            return chosen[name]
        return manifest.get(name, default)

    switches = dict(manifest.get("InstallerSwitches") or {})
    switches.update(chosen.get("InstallerSwitches") or {})
    entries = field("AppsAndFeaturesEntries") or []
    entry = entries[0] if entries else {}
    nested = field("NestedInstallerFiles") or []

    return {
        "file": installer_file.name,
        "winget_type": str(field("InstallerType", "") or "").lower(),
        "nested_type": str(field("NestedInstallerType", "") or "").lower(),
        "nested_files": [n.get("RelativeFilePath", "") for n in nested if isinstance(n, dict)],
        "silent_switch": switches.get("Silent", "") or "",
        "custom_switch": switches.get("Custom", "") or "",
        "product_code": field("ProductCode", "") or entry.get("ProductCode", "") or "",
        "upgrade_code": entry.get("UpgradeCode", "") or "",
        "display_name": entry.get("DisplayName", "") or "",
        "display_version": entry.get("DisplayVersion", "") or "",
        "scope": field("Scope", "") or "",
        "architecture": field("Architecture", "") or "",
        "package_name": manifest.get("PackageName", ""),
        "publisher": manifest.get("Publisher", ""),
        "version": str(manifest.get("PackageVersion", "") or ""),
        "description": manifest.get("ShortDescription", "") or manifest.get("Description", "") or "",
        "information_url": manifest.get("PackageUrl", "") or manifest.get("PublisherUrl", "") or "",
        "privacy_url": manifest.get("PrivacyUrl", "") or "",
        "package_identifier": manifest.get("PackageIdentifier", ""),
    }


def _pick_installer(installers: list[dict], installer_file: Path, architecture: str) -> dict:
    """winget names downloads Name_Version_Scope_Arch_Type_Locale.ext — match on those tokens."""
    tokens = {t.lower() for t in re.split(r"[_\s]+", installer_file.stem)}
    ext = installer_file.suffix.lower().lstrip(".")

    def score(inst: dict) -> int:
        s = 0
        if str(inst.get("Architecture", "")).lower() in tokens:
            s += 4
        if architecture and str(inst.get("Architecture", "")).lower() == architecture.lower():
            s += 2
        if str(inst.get("InstallerType", "")).lower() in tokens:
            s += 3
        if str(inst.get("Scope", "")).lower() in tokens:
            s += 2
        url = str(inst.get("InstallerUrl", "")).lower()
        if url.endswith("." + ext):
            s += 1
        return s

    return max(installers, key=score)


def _load_yaml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError:
            pass
    # Minimal fallback: top-level "Key: value" pairs only
    data: dict = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z]+):\s*(.+)$", line)
        if m:
            data[m.group(1)] = m.group(2).strip().strip("'\"")
    return data


# --------------------------------------------------------------------------- table parsing
def _parse_table(output: str) -> list[dict]:
    """Parse winget's fixed-width table output (column positions from the header line)."""
    lines = [ln.split("\r")[-1].rstrip() for ln in output.splitlines()]
    sep_idx = next((i for i, ln in enumerate(lines) if ln.strip() and set(ln.strip()) == {"-"} and len(ln.strip()) > 10), None)
    if sep_idx is None or sep_idx == 0:
        return []
    header = lines[sep_idx - 1]
    starts = [m.start() for m in re.finditer(r"\S+", header)]
    if len(starts) < 3:
        return []
    rows = []
    for ln in lines[sep_idx + 1:]:
        if not ln.strip():
            continue
        cols = []
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else None
            cols.append(ln[start:end].strip() if end else ln[start:].strip())
        # Columns: Name, Id, Version, [Match], [Source]
        name, pid, ver = cols[0], cols[1], cols[2]
        if not pid or " " in pid:
            # Misaligned row (wide characters) - fall back to splitting on 2+ spaces
            parts = re.split(r"\s{2,}", ln.strip())
            if len(parts) >= 3:
                name, pid, ver = parts[0], parts[1], parts[2]
        rows.append({"name": name, "id": pid, "version": ver, "match": cols[3] if len(cols) > 3 else ""})
    return rows
