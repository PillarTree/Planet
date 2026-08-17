"""APT package model and background operations.

Package listings are read from the ``apt`` (python-apt) cache when available,
falling back to ``apt-cache``/``dpkg`` subprocesses.  Install / remove / upgrade
run on a background thread and report progress through a queue, mirroring the
pattern in Cadet's ``fs`` module so the UI never blocks.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional

try:
    import apt as _apt
    _HAS_PYTHON_APT = True
except Exception:
    _apt = None
    _HAS_PYTHON_APT = False


# ---------------------------------------------------------------------------
# Package model
# ---------------------------------------------------------------------------
@dataclass
class Package:
    name: str
    description: str = ""
    version: str = ""
    installed_version: str = ""
    size: int = 0                # candidate installed size in KiB
    section: str = ""
    homepage: str = ""
    icon_name: str = ""
    origin: str = ""             # repository origin, e.g. "Ubuntu noble main"
    installed: bool = False
    upgradable: bool = False
    is_hidden: bool = False

    @property
    def display_size(self) -> str:
        return format_size(self.size * 1024) if self.size else "\u2014"

    @property
    def display_version(self) -> str:
        return self.version or self.installed_version or ""

    @property
    def status_label(self) -> str:
        if self.installed:
            return "installed"
        if self.upgradable:
            return "upgrade"
        return "available"

    @property
    def status_color(self):
        from . import theme
        if self.installed:
            return theme.INSTALLED
        if self.upgradable:
            return theme.UPGRADE
        return theme.NOT_INST


def format_size(n: int) -> str:
    if n < 0:
        return "?"
    if n < 1024:
        return f"{n} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    v = float(n)
    for u in units:
        v /= 1024.0
        if v < 1024.0:
            return f"{v:.1f} {u}"
    return f"{v:.1f} EiB"


# ---------------------------------------------------------------------------
# Sort keys
# ---------------------------------------------------------------------------
SORT_NAME    = "name"
SORT_SIZE    = "size"
SORT_VERSION = "version"
SORT_STATUS  = "status"

_SORT_FALLBACK = {
    SORT_NAME: lambda e: e.name.lower(),
    SORT_SIZE: lambda e: e.size,
    SORT_VERSION: lambda e: e.version,
    SORT_STATUS: lambda e: (0 if e.installed else 1),
}


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def list_packages(filter: str = "all",
                  show_hidden: bool = False,
                  search: str = "",
                  cache=None) -> List[Package]:
    """Return a list of packages matching ``filter``.

    ``filter`` is one of ``"all"``, ``"installed"``, ``"available"``,
    ``"upgradable"``.
    """
    if _HAS_PYTHON_APT:
        pkgs = _list_via_python_apt(filter, cache)
    else:
        pkgs = _list_via_subprocess(filter)
    if search:
        sl = search.lower()
        pkgs = [p for p in pkgs
                if sl in p.name.lower() or sl in p.description.lower()]
    if not show_hidden:
        pkgs = [p for p in pkgs if not p.is_hidden]
    return sort_packages(pkgs, SORT_NAME, False, True)


def _list_via_python_apt(filter: str, cache=None) -> List[Package]:
    if cache is None:
        cache = _apt.Cache()
        cache.open()
        owned = True
    else:
        owned = False
    try:
        pkgs: List[Package] = []
        for name in cache.keys():
            if name.startswith("."):
                continue
            pkg = cache[name]
            cand = pkg.candidate
            inst = pkg.installed
            if cand is None:
                continue
            installed = inst is not None
            upgradable = installed and pkg.is_upgradable
            if filter == "installed" and not installed:
                continue
            if filter == "available" and installed:
                continue
            if filter == "upgradable" and not upgradable:
                continue
            size = 0
            try:
                size = int(cand.install_size / 1024) if cand else 0
            except Exception:
                size = 0
            section = getattr(cand, "section", "") or ""
            homepage = getattr(cand, "homepage", "") or ""
            desc = getattr(cand, "summary", "") or ""
            icon = _guess_icon(name, section)
            pkgs.append(Package(
                name=name,
                description=desc,
                version=(cand.version or ""),
                installed_version=(inst.version if inst else ""),
                size=size,
                section=section,
                homepage=homepage,
                icon_name=icon,
                origin="",             # lazy-loaded via fetch_origin()
                installed=installed,
                upgradable=upgradable,
            ))
        return pkgs
    finally:
        if owned:
            cache.close()


def _list_via_subprocess(filter: str) -> List[Package]:
    pkgs: List[Package] = []
    try:
        out = subprocess.run(
            ["apt-cache", "pkgnames"],
            capture_output=True, text=True, timeout=30)
        names = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return pkgs
    batch = 200
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        for n in chunk:
            info = _apt_cache_show(n)
            if not info:
                continue
            pkgs.append(info)
    return pkgs


def _apt_cache_show(name: str) -> Optional[Package]:
    try:
        out = subprocess.run(
            ["apt-cache", "show", name],
            capture_output=True, text=True, timeout=10)
        text = out.stdout
    except Exception:
        return None
    if not text:
        return None
    fields = {}
    for para in text.split("\n\n"):
        f = {}
        cur_key = None
        val = ""
        for line in para.splitlines():
            if line.startswith(" "):
                if cur_key:
                    val += "\n" + line.strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                if cur_key:
                    f[cur_key] = val.strip()
                cur_key = k.strip()
                val = v.strip()
            else:
                if cur_key:
                    val += "\n" + line.strip()
        if cur_key:
            f[cur_key] = val.strip()
        if "Package" in f:
            fields = f
            break
    installed = False
    out = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status} ${Version}", name],
        capture_output=True, text=True, timeout=5)
    if out.returncode == 0:
        parts = out.stdout.split()
        installed = "install ok installed" in out.stdout
        inst_ver = parts[-1] if parts else ""
    else:
        inst_ver = ""
    size = 0
    for k in ("Installed-Size", "Size"):
        if k in fields:
            try:
                size = int(fields[k])
            except ValueError:
                pass
    return Package(
        name=fields.get("Package", name),
        description=fields.get("Description", ""),
        version=fields.get("Version", ""),
        installed_version=inst_ver,
        size=size,
        section=fields.get("Section", ""),
        homepage=fields.get("Homepage", ""),
        icon_name=_guess_icon(name, fields.get("Section", "")),
        installed=installed,
    )


def _guess_icon(name: str, section: str) -> str:
    """Pick a reasonable freedesktop icon name for the package."""
    sec = section.lower() if section else ""
    if "graphics" in sec or "photo" in sec:
        return "applications-graphics"
    if "sound" in sec or "video" in sec or "multimedia" in sec:
        return "applications-multimedia"
    if "game" in sec:
        return "applications-games"
    if "network" in sec or "web" in sec:
        return "applications-internet"
    if "devel" in sec or "lib" in sec:
        return "applications-development"
    if "text" in sec or "edit" in sec:
        return "applications-editing"
    if "science" in sec or "math" in sec:
        return "applications-science"
    if "office" in sec:
        return "application-office"
    if "system" in sec:
        return "applications-system"
    return "package-x-generic"


def _format_origin(origins) -> str:
    """Return a human-readable repository origin string from python-apt origins."""
    if not origins:
        return ""
    best = origins[0]
    for o in origins:
        if o.label and o.archive:
            best = o
            break
    parts = [p for p in [best.label, best.archive, best.component] if p]
    return " ".join(parts) if parts else best.site or ""


def fetch_origin(name: str, cache=None) -> str:
    """Fetch the repository origin for a single package (lazy-loaded).

    ``list_packages`` skips the expensive ``cand.origins`` call for every
    package, which would take ~50 s for 90 k+ entries.  Instead, the origin is
    fetched on-demand for the handful of packages actually displayed.
    """
    if not _HAS_PYTHON_APT:
        return ""
    owned = False
    try:
        if cache is None:
            cache = _apt.Cache()
            cache.open()
            owned = True
        pkg = cache.get(name)
        if pkg is None:
            return ""
        cand = pkg.candidate
        if cand is None:
            return ""
        return _format_origin(cand.origins)
    except Exception:
        return ""
    finally:
        if owned:
            try:
                cache.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# APT sources listing
# ---------------------------------------------------------------------------
@dataclass
class Source:
    name: str                  # human-readable label
    uri: str                   # e.g. https://download.docker.com/linux/ubuntu
    suites: List[str]          # e.g. ["noble", "noble-updates"]
    components: List[str]      # e.g. ["main", "stable"]
    enabled: bool = True
    filename: str = ""


def list_sources() -> List[Source]:
    """Parse /etc/apt/sources.list and sources.list.d/ for configured repos."""
    sources: List[Source] = []
    main_file = "/etc/apt/sources.list"
    if os.path.isfile(main_file):
        sources.extend(_parse_source_file(main_file))
    sd = "/etc/apt/sources.list.d"
    if os.path.isdir(sd):
        for fn in sorted(os.listdir(sd)):
            fp = os.path.join(sd, fn)
            if not os.path.isfile(fp):
                continue
            if fn.endswith(".sources"):
                sources.extend(_parse_deb822_sources(fp))
            elif fn.endswith(".list"):
                sources.extend(_parse_source_file(fp))
    return sources


def _parse_source_file(path: str) -> List[Source]:
    sources: List[Source] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                src = _parse_one_line(line, path)
                if src:
                    sources.append(src)
    except OSError:
        pass
    return sources


def _parse_one_line(line: str, filename: str) -> Optional[Source]:
    """Parse a single ``deb`` / ``deb-src`` line in one-line format."""
    import re
    rest = line
    enabled = True
    if rest.startswith("#"):
        enabled = False
        rest = rest.lstrip("#").strip()
    if not (rest.startswith("deb ") or rest.startswith("deb-src ")
            or rest.startswith("deb\t") or rest.startswith("deb-src\t")):
        return None
    m = re.match(r"^(deb(?:-src)?)\s*(?:\[([^\]]*)\]\s*)?(\S+)\s+(.+)$", rest)
    if not m:
        return None
    opts_str = m.group(2) or ""
    opts = {}
    for kv in opts_str.split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            opts[k] = v
    uri = m.group(3)
    suite_parts = m.group(4).split()
    if not suite_parts:
        return None
    suite = suite_parts[0]
    components = suite_parts[1:] if len(suite_parts) > 1 else []
    name = opts.get("label", "")
    if not name and uri:
        name = uri.split("/")[2] if len(uri.split("/")) > 2 else uri
    if not name:
        name = os.path.basename(filename)
    return Source(name=name, uri=uri, suites=[suite],
                  components=components, enabled=enabled, filename=filename)


def _parse_deb822_sources(path: str) -> List[Source]:
    """Parse a Deb822-format ``.sources`` file."""
    try:
        with open(path) as f:
            content = f.read()
    except OSError:
        return []
    sources: List[Source] = []
    for stanza in content.split("\n\n"):
        fields: dict = {}
        for line in stanza.splitlines():
            if line.startswith(" ") or line.startswith("\t"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        if not fields:
            continue
        types = fields.get("types", "deb").split()
        uris = fields.get("uris", "").split()
        suites = fields.get("suites", "").split()
        components = fields.get("components", "main").split()
        enabled = fields.get("enabled", "true").lower() == "true"
        name = fields.get("x-repolib-name", "") or os.path.basename(path)
        for uri in uris:
            for suite in suites:
                sources.append(Source(
                    name=name, uri=uri.rstrip("/"),
                    suites=[suite], components=components,
                    enabled=enabled, filename=path))
    return sources


def sort_packages(entries: List[Package], sort_key: str,
                  dirs_first: bool = False, reverse: bool = False) -> List[Package]:
    key = _SORT_FALLBACK.get(sort_key, _SORT_FALLBACK[SORT_NAME])
    return sorted(entries, key=key, reverse=reverse)


def upgrade_count(cache=None) -> int:
    """Count packages with an upgrade available."""
    if not _HAS_PYTHON_APT:
        return 0
    if cache is None:
        cache = _apt.Cache()
        cache.upgradable = True
        n = len([p for p in cache if cache[p].is_upgradable])
        cache.close()
        return n
    return len([p for p in cache if cache[p].is_upgradable])


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
class OpError(Exception):
    pass


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
Message = tuple  # (kind, payload)
# kinds: "progress" (str), "done" (result), "error" (str),
#        "status" (str, color), "refresh" (None)


class Worker:
    def __init__(self, messages: "queue.Queue[Message]"):
        self._q = messages
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False
        self.busy = False

    def run(self, fn: Callable[..., object], *args):
        if self.busy:
            raise OpError("Another operation is already running")
        self._cancelled = False
        self.busy = True
        self._thread = threading.Thread(
            target=self._wrap, args=(fn, args), daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _wrap(self, fn, args):
        try:
            fn(*args)
            self._q.put(("done", None))
        except OpError as e:
            self._q.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._q.put(("error", f"{e}"))
        finally:
            self.busy = False


# ---------------------------------------------------------------------------
# Operations (run on worker thread)
# ---------------------------------------------------------------------------
def _progress(q, text: str):
    q.put(("progress", text))


def _need_root():
    return os.geteuid() != 0


def op_install(names: List[str], messages, cancel_holder) -> None:
    if _need_root():
        raise OpError("Installing packages requires root privileges. "
                      "Run planet with sudo, or install via apt directly.")
    cmd = ["apt-get", "install", "-y"]
    for n in names:
        if cancel_holder[0]:
            raise OpError("Cancelled")
        _progress(messages, f"Preparing \u201c{n}\u201d\u2026")
        cmd.append(n)
    _run_apt(cmd, messages, cancel_holder, prefix="Installing ")


def op_remove(names: List[str], messages, cancel_holder, purge: bool = False,
              remove_deps: bool = True) -> None:
    if _need_root():
        raise OpError("Removing packages requires root privileges.")
    verb = "Purging" if purge else "Removing"
    sub = "purge" if purge else "remove"
    cmd = ["apt-get", sub, "-y"]
    if remove_deps:
        cmd.append("--auto-remove")
    for n in names:
        if cancel_holder[0]:
            raise OpError("Cancelled")
        _progress(messages, f"{verb} \u201c{n}\u2026")
        cmd.append(n)
    _run_apt(cmd, messages, cancel_holder, prefix=verb + " ")


def op_upgrade(messages, cancel_holder) -> None:
    if _need_root():
        raise OpError("Upgrading packages requires root privileges.")
    _progress(messages, "Fetching package lists\u2026")
    _run(["apt-get", "update"], messages, cancel_holder,
         label="Updating package index\u2026")
    if cancel_holder[0]:
        raise OpError("Cancelled")
    _run(["apt-get", "upgrade", "-y"], messages, cancel_holder,
         label="Upgrading installed packages\u2026")


def op_apt_update(messages, cancel_holder) -> None:
    """Fetch fresh package lists from all configured APT sources.

    This is what ``apt-get update`` does — it contacts every repository in
    ``/etc/apt/sources.list(.d/)`` over the internet and refreshes the local
    package index so that ``list_packages`` sees the latest versions.
    """
    if _need_root():
        raise OpError("Updating package lists requires root privileges. "
                      "Run Planet with sudo.")
    _run(["apt-get", "update"], messages, cancel_holder,
         label="Fetching package lists from sources\u2026")


def _run_apt(cmd: List[str], messages, cancel_holder, prefix: str):
    """Run an apt-get subcommand and stream simplified progress lines."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
    except FileNotFoundError:
        raise OpError("apt-get is not available on this system")
    last_pct = -1
    try:
        for line in proc.stdout:
            if cancel_holder[0]:
                proc.terminate()
                raise OpError("Cancelled")
            ll = line.strip()
            if ll:
                if "Setting up" in ll or "Unpacking" in ll or "Removing" in ll:
                    _progress(messages, ll)
                elif ll.startswith("E:"):
                    raise OpError(ll)
        rc = proc.wait()
        if rc != 0:
            raise OpError(f"apt-get exited with code {rc}")
    except OpError:
        raise
    except Exception as e:
        raise OpError(str(e))


def _run(cmd: List[str], messages, cancel_holder, label: str = "",
         timeout: int = 600):
    """Run a blocking command, reporting progress through the queue."""
    _progress(messages, label)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OpError(f"Timed out running \u201c{' '.join(cmd)}\u201d")
    except FileNotFoundError:
        raise OpError(f"\u201c{' '.join(cmd)}\u201d: command not found")
    if proc.returncode != 0 and proc.stdout:
        raise OpError(proc.stdout.strip()[:500])


def op_reveal(path: str) -> None:
    try:
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass
