#!/usr/bin/env python3
"""
DeckPort - Obtainium-style updater for AppImages & PC ports on Steam Deck.

Track GitHub Releases, check for updates, and download/replace them in one place.
Pure Python standard library: no pip installs needed on SteamOS.

Usage:
  deckport.py serve                 # open the web UI (default)
  deckport.py add <github-url>      # track a repo
  deckport.py list
  deckport.py check
  deckport.py update [name ...]     # update given apps, or everything with an update
  deckport.py remove <name>
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.2.0"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "deckport"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PORT = 8765
USER_AGENT = "DeckPort/" + VERSION

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")
# Assets that are clearly not for a Steam Deck (x86_64 Linux).
NOT_FOR_DECK = re.compile(
    r"(windows|win32|win64|[-_.]win[-_.]|\.exe$|\.msi$|macos|mac[-_.]?os|darwin|osx|\.dmg$|\.pkg$"
    r"|android|\.apk$|[-_.]ios[-_.]|aarch64|arm64|armhf|armv\d|[-_.]arm([-_.]?\d+|el)?([-_. )]|$)|i386|i686|riscv|ppc64|s390x|\.zsync$|\.sha\d*(sum)?$|\.sig$|\.asc$"
    r"|checksums?|\.blockmap$|\.txt$|\.json$|\.ya?ml$|\.deb$|\.rpm$|flatpak|\.snap$|debug|symbols|\.pdb$)",
    re.I,
)


# --------------------------------------------------------------------------- config

_lock = threading.RLock()


def default_config():
    return {
        "settings": {
            "install_dir": str(Path.home() / "Applications"),
            "github_token": "",
            "keep_backup": True,
        },
        "apps": [],
    }


def load_config():
    with _lock:
        cfg = default_config()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                cfg["settings"].update(data.get("settings", {}))
                cfg["apps"] = data.get("apps", [])
                for a in cfg["apps"]:  # configs from before multi-forge support are GitHub-only
                    a.setdefault("provider", "github")
                    a.setdefault("host", "github.com")
            except (OSError, ValueError) as e:
                print("Warning: could not read config (%s), starting fresh" % e, file=sys.stderr)
        return cfg


def save_config(cfg):
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.chmod(tmp, 0o600)  # may contain a GitHub token
        os.replace(tmp, CONFIG_FILE)


def find_app(cfg, key):
    key_l = key.lower()
    for a in cfg["apps"]:
        if a["id"] == key or key_l in (a["name"].lower(), a["repo"].lower(), display_repo(a).lower()):
            return a
    return None


# --------------------------------------------------------------------------- forges (GitHub, GitLab, Gitea/Forgejo)

PROVIDERS = ("github", "gitlab", "gitea")
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea"}


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects (GitHub assets bounce to a CDN) but never downgrade to plain HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "Refusing non-HTTPS redirect", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Tokens are only for the API host, never for wherever we get redirected to.
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def valid_repo(repo):
    parts = repo.split("/") if isinstance(repo, str) else []
    return len(parts) >= 2 and all(re.fullmatch(r"[\w.-]+", p) and p not in (".", "..") for p in parts)


def parse_source(text):
    """Return (provider or None, host, repo_path) for owner/repo or a forge URL."""
    text = text.strip()
    m = re.match(r"^git@github\.com:([\w.-]+/[\w.-]+?)(\.git)?$", text)
    if m:
        return "github", "github.com", m.group(1)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text) and valid_repo(text):
        return "github", "github.com", re.sub(r"\.git$", "", text)
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    u = urllib.parse.urlsplit(text)
    if u.scheme.lower() != "https" or not u.hostname:
        raise ValueError("Use an https:// link to a GitHub, GitLab, Codeberg, Gitea or Forgejo repo")
    host = u.hostname.lower()
    parts = [p for p in u.path.split("/") if p]
    provider = KNOWN_HOSTS.get(host)
    if "-" in parts and parts.index("-") >= 2:  # GitLab style: /group/sub/project/-/releases
        parts = parts[:parts.index("-")]
        provider = provider or "gitlab"
    elif provider in ("github", "gitea"):
        parts = parts[:2]
    elif "releases" in parts and parts.index("releases") >= 2:
        parts = parts[:parts.index("releases")]
    repo = re.sub(r"\.git$", "", "/".join(parts))
    if not valid_repo(repo):
        raise ValueError("That link doesn't point to a repository: %s" % text)
    return provider, host, repo


def http_json(url, token="", service=""):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener.open(req, timeout=30) as r:
            return json.loads(r.read(20 * 1024 * 1024).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError("not found")
        if e.code in (403, 429):
            if service == "github":
                reset = e.headers.get("X-RateLimit-Reset")
                when = " (resets in %d min)" % max(1, (int(reset) - time.time()) // 60) if reset else ""
                raise RuntimeError("GitHub rate limit hit%s. Add a GitHub token in Settings." % when)
            raise RuntimeError("%s refused the request (HTTP %d), maybe rate limited" % (urllib.parse.urlsplit(url).hostname, e.code))
        raise RuntimeError("API error %d from %s" % (e.code, urllib.parse.urlsplit(url).hostname))
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: %s" % e.reason)
    except ValueError:
        raise RuntimeError("%s did not return valid JSON" % urllib.parse.urlsplit(url).hostname)


def detect_provider(host, repo):
    """Self-hosted forge: ask the Gitea/Forgejo API, then GitLab's, which one answers."""
    try:
        if "full_name" in http_json("https://%s/api/v1/repos/%s" % (host, repo)):
            return "gitea"
    except (LookupError, RuntimeError):
        pass
    try:
        if "path_with_namespace" in http_json("https://%s/api/v4/projects/%s" % (host, urllib.parse.quote(repo, safe=""))):
            return "gitlab"
    except (LookupError, RuntimeError):
        pass
    raise RuntimeError("Couldn't find %s on %s as a GitHub, GitLab, Gitea or Forgejo repo" % (repo, host))


def _sha256_of(digest):
    d = (digest or "").strip().lower()
    d = d[7:] if d.startswith("sha256:") else d
    return d if re.fullmatch(r"[0-9a-f]{64}", d) else ""


def _norm_github(r):
    return {
        "tag_name": r.get("tag_name") or "", "name": r.get("name") or "",
        "published_at": r.get("published_at") or "", "html_url": r.get("html_url") or "",
        "prerelease": bool(r.get("prerelease")), "draft": bool(r.get("draft")),
        "assets": [{
            "id": a["id"], "name": a["name"], "size": a.get("size", 0),
            "updated_at": a.get("updated_at", ""), "browser_download_url": a["browser_download_url"],
            "sha256": _sha256_of(a.get("digest")),  # GitHub publishes "sha256:<hex>" per asset
        } for a in r.get("assets", [])],
    }


def _norm_gitea(r):
    return {
        "tag_name": r.get("tag_name") or "", "name": r.get("name") or "",
        "published_at": r.get("published_at") or r.get("created_at") or "", "html_url": r.get("html_url") or "",
        "prerelease": bool(r.get("prerelease")), "draft": bool(r.get("draft")),
        "assets": [{
            "id": a.get("uuid") or a["id"], "name": a["name"], "size": a.get("size", 0),
            "updated_at": a.get("created_at", ""), "browser_download_url": a["browser_download_url"],
            "sha256": "",
        } for a in r.get("assets", [])],
    }


def _norm_gitlab(r, host, repo):
    links = (r.get("assets") or {}).get("links") or []
    when = r.get("released_at") or r.get("created_at") or ""
    return {
        "tag_name": r.get("tag_name") or "", "name": r.get("name") or "",
        "published_at": when,
        "html_url": ((r.get("_links") or {}).get("self")) or "https://%s/%s/-/releases" % (host, repo),
        "prerelease": bool(r.get("upcoming_release")), "draft": False,
        "assets": [{
            "id": l["id"], "name": l["name"], "size": 0, "updated_at": when,
            "browser_download_url": l.get("direct_asset_url") or l["url"], "sha256": "",
        } for l in links],
    }


def fetch_release(src, prerelease, token=""):
    """Latest release of src={provider, host, repo}, normalized to one shape for all forges."""
    provider, host, repo = src["provider"], src["host"], src["repo"]
    try:
        if provider == "github":
            api = "https://api.github.com/repos/%s/releases" % repo
            if not prerelease:
                try:
                    return _norm_github(http_json(api + "/latest", token, "github"))
                except LookupError:
                    pass  # repo may only have pre-releases; fall through to the list
            rels = [_norm_github(r) for r in http_json(api + "?per_page=15", token, "github")]
        elif provider == "gitea":
            rels = [_norm_gitea(r) for r in http_json("https://%s/api/v1/repos/%s/releases?limit=15" % (host, repo))]
        elif provider == "gitlab":
            pid = urllib.parse.quote(repo, safe="")
            rels = [_norm_gitlab(r, host, repo) for r in http_json("https://%s/api/v4/projects/%s/releases?per_page=15" % (host, pid))]
        else:
            raise RuntimeError("Unknown provider %r" % provider)
    except LookupError:
        raise RuntimeError("Repo %s not found on %s" % (repo, host))
    rels = [r for r in rels if not r["draft"]]
    if not prerelease:
        rels = [r for r in rels if not r["prerelease"]] or rels
    if not rels:
        raise RuntimeError("No releases found in %s" % repo)
    return rels[0]


def web_url(app):
    base = "https://%s/%s" % (app.get("host", "github.com"), app["repo"])
    return base + ("/-/releases" if app.get("provider") == "gitlab" else "/releases")


def display_repo(app):
    return app["repo"] if app.get("host", "github.com") == "github.com" else "%s/%s" % (app["host"], app["repo"])


def score_asset(name):
    n = name.lower()
    if NOT_FOR_DECK.search(n):
        return -1
    s = 0
    if n.endswith(".appimage"):
        s += 100
    elif n.endswith(ARCHIVE_EXTS):
        s += 10
    if "linux" in n:
        s += 30
    if re.search(r"steam[-_. ]?deck|steamos", n):
        s += 40
    if re.search(r"(^|[^a-z0-9])(x86[_-]?64|amd64|x64)($|[^a-z0-9])", n):
        s += 10
    return s


def pick_asset(assets, pattern=""):
    if pattern:
        rx = re.compile(pattern, re.I)
        matches = [a for a in assets if rx.search(a["name"])]
        return matches[0] if matches else None
    scored = [(score_asset(a["name"]), a) for a in assets]
    scored = [x for x in scored if x[0] >= 30]
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def find_checksum_asset(assets, name):
    """A checksum file published next to the asset: (asset, is_specific_to_this_file)."""
    by_name = {a["name"].lower(): a for a in assets}
    for suffix in (".sha256", ".sha256sum", ".sha256.txt"):
        if (name + suffix).lower() in by_name:
            return by_name[(name + suffix).lower()], True
    for a in assets:
        n = a["name"].lower()
        if re.search(r"sha-?256|checksums?", n) and not re.search(r"sha-?(1|384|512)\b|md5|\.sig$|\.asc$", n):
            return a, False
    return None, False


def release_info(rel, asset):
    info = {
        "tag": rel.get("tag_name") or "",
        "title": rel.get("name") or rel.get("tag_name") or "",
        "published": rel.get("published_at") or "",
        "url": rel.get("html_url") or "",
        "prerelease": bool(rel.get("prerelease")),
        "asset": None,
    }
    if asset:
        info["asset"] = {
            "id": asset["id"],
            "name": asset["name"],
            "size": asset.get("size", 0),
            "updated_at": asset.get("updated_at", ""),
            "download_url": asset["browser_download_url"],
            "sha256": asset.get("sha256", ""),
        }
        if not info["asset"]["sha256"]:
            ck, specific = find_checksum_asset(rel.get("assets", []), asset["name"])
            if ck:
                info["asset"]["checksum_url"] = ck["browser_download_url"]
                info["asset"]["checksum_specific"] = specific
    return info


def has_update(app):
    latest = app.get("latest") or {}
    la = latest.get("asset")
    if not la:
        return False
    inst = app.get("installed")
    if not inst or not inst.get("path") or not Path(inst["path"]).exists():
        return True
    # Asset id + timestamp also catches "nightly"/"continuous" tags that get re-uploaded.
    return inst.get("asset_id") != la["id"] or inst.get("updated_at") != la["updated_at"]


def check_app(app, token=""):
    try:
        rel = fetch_release(app, app.get("prerelease", False), token if app.get("provider") == "github" else "")
        asset = pick_asset(rel.get("assets", []), app.get("pattern", ""))
        app["latest"] = release_info(rel, asset)
        app["error"] = "" if asset else "No matching Linux asset in release %s. Set an asset pattern." % rel.get("tag_name")
    except Exception as e:
        app["error"] = str(e)
    app["checked_at"] = time.time()
    return has_update(app)


# --------------------------------------------------------------------------- install

def safe_name(name):
    return re.sub(r"[^\w.-]+", "-", name).strip("-") or "app"


def asset_kind(name):
    n = name.lower()
    if n.endswith(".appimage"):
        return "appimage"
    if n.endswith(ARCHIVE_EXTS):
        return "archive"
    return "file"


def sniff_kind(path, name):
    """Kind from the file's magic bytes; GitLab asset names are often labels like "Linux (x86_64)"."""
    with open(path, "rb") as f:
        head = f.read(512)
    if head[:4] == b"\x7fELF":
        return "appimage" if head[8:10] == b"AI" or name.lower().endswith(".appimage") else "file"
    if (head[:4] == b"PK\x03\x04" or head[:2] == b"\x1f\x8b" or head[:6] == b"\xfd7zXZ\x00"
            or head[:3] == b"BZh" or head[257:262] == b"ustar"):
        return "archive"
    return asset_kind(name)


def _open_download(url):
    if not url.lower().startswith("https://"):
        raise RuntimeError("Refusing to download over plain HTTP: %s" % url)
    return _opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=60)


def download(url, dest, progress=None):
    """Stream url to dest and return the file's SHA-256."""
    h = hashlib.sha256()
    with _open_download(url) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


def expected_sha256(asset):
    """Published SHA-256 for the asset: from the API, else from a checksum file in the release."""
    if asset.get("sha256"):
        return asset["sha256"], "published by the forge"
    url = asset.get("checksum_url")
    if not url:
        return "", ""
    try:
        with _open_download(url) as r:
            text = r.read(1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return "", ""
    first = ""
    for line in text.splitlines():
        m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])", line)
        if not m:
            continue
        first = first or m.group(1).lower()
        if asset["name"] in line:
            return m.group(1).lower(), "checksum file"
    if asset.get("checksum_specific") and first:
        return first, "checksum file"
    return "", ""


def _inside(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)


def extract_archive(path, out_dir):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not _inside(out_dir, os.path.join(out_dir, info.filename)):
                    raise RuntimeError("Unsafe path in archive: %s" % info.filename)
                z.extract(info, out_dir)
                mode = info.external_attr >> 16
                if mode and not info.is_dir():
                    os.chmod(os.path.join(out_dir, info.filename), mode & 0o777)
    else:
        with tarfile.open(path) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(out_dir, filter="data")
            else:
                for m in t.getmembers():
                    target = os.path.join(out_dir, m.name)
                    link_ok = not (m.issym() or m.islnk()) or _inside(
                        out_dir, os.path.join(os.path.dirname(target), m.linkname))
                    if not _inside(out_dir, target) or not link_ok or m.isdev():
                        raise RuntimeError("Unsafe path in archive: %s" % m.name)
                t.extractall(out_dir)


def copy_over(src, dst):
    """Copy src tree onto dst, overwriting files but keeping extra files (saves, configs)."""
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target_root, exist_ok=True)
        for fn in files:
            s = os.path.join(root, fn)
            d = os.path.join(target_root, fn)
            if os.path.islink(d) or os.path.isfile(d):
                os.remove(d)
            shutil.copy2(s, d, follow_symlinks=False)


def make_executable(p):
    os.chmod(p, os.stat(p).st_mode | 0o755)


def install_app(app, settings, progress=None, phase=None):
    latest = app.get("latest") or {}
    asset = latest.get("asset")
    if not asset:
        raise RuntimeError(app.get("error") or "Nothing to install, run a check first")

    install_dir = Path(os.path.expanduser(settings["install_dir"]))
    install_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(app["name"])
    work = Path(tempfile.mkdtemp(prefix=".deckport-", dir=install_dir))
    try:
        dl = work / safe_name(asset["name"])
        actual = download(asset["download_url"], dl, progress)
        if phase:
            phase("Verifying")
        expected, source = expected_sha256(asset)
        if expected and expected != actual:
            raise RuntimeError("SHA-256 mismatch for %s: expected %s, got %s. The download was deleted."
                               % (asset["name"], expected, actual))
        kind = sniff_kind(dl, asset["name"])

        if kind == "archive":
            ex = work / "extracted"
            ex.mkdir()
            extract_archive(dl, str(ex))
            dl.unlink()
            # Many ports ship a zip that only wraps a tar.gz (to keep exec bits): unwrap it.
            for depth in range(3):
                inner = list(ex.iterdir())
                if len(inner) != 1 or not inner[0].is_file() or sniff_kind(inner[0], inner[0].name) != "archive":
                    break
                nxt = work / ("extracted%d" % depth)
                nxt.mkdir()
                extract_archive(inner[0], str(nxt))
                shutil.rmtree(ex)
                ex = nxt
            appimages = sorted(ex.rglob("*.[Aa]pp[Ii]mage"))
            if appimages:
                dl, kind = appimages[0], "appimage"
            else:
                # Strip a single versioned top-level folder (e.g. "MyGame-v1.2/") so the
                # layout stays stable across updates and Steam shortcuts keep working.
                entries = list(ex.iterdir())
                root = entries[0] if len(entries) == 1 and entries[0].is_dir() else ex
                dest_dir = install_dir / base
                copy_over(str(root), str(dest_dir))
                path = str(dest_dir)

        if kind in ("appimage", "file"):
            suffix = ".AppImage" if kind == "appimage" else ""
            dest = install_dir / (base + suffix)
            make_executable(dl)
            if dest.exists() and settings.get("keep_backup", True):
                os.replace(dest, str(dest) + ".old")
            shutil.move(str(dl), str(dest))  # same filesystem, so this is an atomic rename
            path = str(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    app["installed"] = {
        "tag": latest["tag"],
        "asset": asset["name"],
        "asset_id": asset["id"],
        "updated_at": asset["updated_at"],
        "kind": kind,
        "path": path,
        "date": time.time(),
        "sha256": actual,
        "verified": bool(expected),
        "verified_by": source,
    }
    app["error"] = ""
    return path


def rollback_app(app):
    inst = app.get("installed") or {}
    p = inst.get("path")
    if not p or not os.path.exists(p + ".old"):
        raise RuntimeError("No backup available for %s" % app["name"])
    os.replace(p + ".old", p)
    # Forget the installed version so the next check offers the update again.
    inst["asset_id"] = None
    inst["tag"] = (inst.get("tag") or "") + " (rolled back)"


def remove_app(cfg, app, delete_files=False):
    if delete_files:
        p = (app.get("installed") or {}).get("path")
        if p and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif p:
            for f in (p, p + ".old"):
                if os.path.exists(f):
                    os.remove(f)
    cfg["apps"] = [a for a in cfg["apps"] if a["id"] != app["id"]]


def resolve_source(text):
    provider, host, repo = parse_source(text)
    return {"provider": provider or detect_provider(host, repo), "host": host, "repo": repo}


def same_source(a, b):
    return (a.get("provider"), a.get("host", "").lower(), a["repo"].lower(), a.get("pattern", "")) == \
           (b.get("provider"), b.get("host", "").lower(), b["repo"].lower(), b.get("pattern", ""))


def new_app(repo_text, name="", pattern="", prerelease=False, src=None):
    src = src or resolve_source(repo_text)
    if pattern:
        re.compile(pattern)  # validate early
    return {
        "id": uuid.uuid4().hex[:10],
        "name": name.strip() or src["repo"].split("/")[-1],
        "provider": src["provider"],
        "host": src["host"],
        "repo": src["repo"],
        "pattern": pattern,
        "prerelease": bool(prerelease),
        "installed": None,
        "latest": None,
        "error": "",
        "checked_at": 0,
    }


EXPORT_FORMAT = "deckport-apps"
EXPORT_FIELDS = ("name", "provider", "host", "repo", "pattern", "prerelease")


def export_apps(cfg):
    """App list to share or back up: no tokens, local paths or install state."""
    return {
        "format": EXPORT_FORMAT,
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": [{k: a.get(k) for k in EXPORT_FIELDS} for a in cfg["apps"]],
    }


def import_apps(cfg, data):
    """Merge an export into cfg. Validates everything first, so a bad file changes nothing."""
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT or not isinstance(data.get("apps"), list):
        raise ValueError("This is not a DeckPort export file")
    incoming = []
    for i, item in enumerate(data["apps"], 1):
        if not isinstance(item, dict):
            raise ValueError("Entry %d is not an app" % i)
        provider, host, repo = item.get("provider"), item.get("host"), item.get("repo")
        pattern, name = item.get("pattern") or "", item.get("name") or ""
        if provider not in PROVIDERS:
            raise ValueError("Entry %d: unknown provider %r" % (i, provider))
        if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)+(:\d+)?", host.lower()):
            raise ValueError("Entry %d: invalid host %r" % (i, host))
        if not valid_repo(repo):
            raise ValueError("Entry %d: invalid repo %r" % (i, repo))
        if not isinstance(pattern, str) or not isinstance(name, str):
            raise ValueError("Entry %d: invalid name or pattern" % i)
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError("Entry %d: invalid pattern (%s)" % (i, e))
        src = {"provider": provider, "host": host.lower(), "repo": repo}
        incoming.append(new_app("", name[:100], pattern, bool(item.get("prerelease")), src))
    added = []
    for app in incoming:
        if not any(same_source(a, app) for a in cfg["apps"] + added):
            added.append(app)
    cfg["apps"].extend(added)
    return added, len(incoming) - len(added)


def open_path(p):
    opener = "xdg-open" if sys.platform.startswith("linux") else "open"
    subprocess.Popen([opener, p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def launch_app(app):
    inst = app.get("installed") or {}
    p = inst.get("path")
    if not p or not os.path.exists(p):
        raise RuntimeError("Not installed")
    if os.path.isdir(p):
        open_path(p)
    else:
        subprocess.Popen([p], cwd=os.path.dirname(p), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


# --------------------------------------------------------------------------- web server

class Jobs:
    """Background tasks (check/install) with progress the UI can poll."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def start(self, app_id, label, fn):
        with self.lock:
            cur = self.items.get(app_id)
            if cur and cur["state"] == "running":
                return False
            job = {"state": "running", "label": label, "progress": 0.0, "message": ""}
            self.items[app_id] = job

        def run():
            try:
                fn(job)
                job["state"] = "done"
            except Exception as e:
                job["state"] = "error"
                job["message"] = str(e)
            job["finished"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return True

    def snapshot(self):
        with self.lock:
            now = time.time()
            # Drop finished jobs after a while so the UI doesn't show stale results.
            for k in [k for k, j in self.items.items() if j.get("finished") and now - j["finished"] > 8]:
                del self.items[k]
            return {k: dict(v) for k, v in self.items.items()}


def _config_mtime():
    try:
        return CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return None


class Server:
    """App state + actions shared by the web UI and the Decky plugin."""

    def __init__(self):
        self.cfg = load_config()
        self._mtime = _config_mtime()
        self.jobs = Jobs()
        self.token = secrets.token_urlsafe(24)
        self.last_seen = time.time()

    def save(self):
        save_config(self.cfg)
        self._mtime = _config_mtime()

    def busy(self):
        return any(j["state"] == "running" for j in self.jobs.snapshot().values())

    def sync(self):
        """Pick up changes saved by another DeckPort (desktop UI, CLI, Decky plugin) while idle."""
        with _lock:
            m = _config_mtime()
            if m != self._mtime and not self.busy():
                self.cfg = load_config()
                self._mtime = m

    def gh_token(self):
        return self.cfg["settings"].get("github_token", "")

    def state(self):
        with _lock:
            apps = []
            for a in self.cfg["apps"]:
                d = dict(a)
                d["has_update"] = has_update(a)
                d["web_url"] = web_url(a)
                d["display_repo"] = display_repo(a)
                inst = a.get("installed") or {}
                d["has_backup"] = bool(inst.get("path") and os.path.exists(inst["path"] + ".old"))
                d["exists"] = bool(inst.get("path") and os.path.exists(inst["path"]))
                apps.append(d)
            s = dict(self.cfg["settings"])
            s["has_token"] = bool(s.pop("github_token", ""))
            return {"apps": apps, "settings": s, "jobs": self.jobs.snapshot(), "version": VERSION}

    def check(self, app):
        def fn(job):
            check_app(app, self.gh_token())
            self.save()
            if app.get("error"):
                raise RuntimeError(app["error"])
        return self.jobs.start(app["id"], "Checking", fn)

    def install(self, app):
        def fn(job):
            if not app.get("latest") or not app["latest"].get("asset"):
                check_app(app, self.gh_token())
            job["label"] = "Downloading"

            def prog(done, total):
                job["progress"] = (done / total) if total else 0
                job["message"] = "%.1f / %.1f MB" % (done / 1e6, total / 1e6) if total else "%.1f MB" % (done / 1e6)

            def phase(label):
                job["label"], job["message"] = label, ""

            install_app(app, self.cfg["settings"], prog, phase)
            self.save()
        return self.jobs.start(app["id"], "Downloading", fn)

    def handle(self, path, body):
        self.sync()
        cfg = self.cfg
        if path == "/api/state":
            return self.state()

        if path == "/api/preview":
            src = resolve_source(body.get("repo", ""))
            rel = fetch_release(src, body.get("prerelease", False),
                                self.gh_token() if src["provider"] == "github" else "")
            pattern = body.get("pattern", "")
            assets = rel.get("assets", [])
            picked = pick_asset(assets, pattern)
            return {
                "repo": display_repo(src),
                "provider": src["provider"],
                "tag": rel.get("tag_name"),
                "assets": [{"name": a["name"], "size": a.get("size", 0)} for a in assets],
                "picked": picked["name"] if picked else None,
            }

        if path == "/api/add":
            app = new_app(body.get("repo", ""), body.get("name", ""), body.get("pattern", ""),
                          body.get("prerelease", False))
            with _lock:
                if any(same_source(a, app) for a in cfg["apps"]):
                    raise ValueError("%s is already tracked" % display_repo(app))
                cfg["apps"].append(app)
                self.save()
            self.check(app)
            return {"ok": True, "id": app["id"]}

        if path == "/api/edit":
            app = self._app(body)
            with _lock:
                if "name" in body and body["name"].strip():
                    app["name"] = body["name"].strip()
                if "pattern" in body:
                    if body["pattern"]:
                        re.compile(body["pattern"])
                    app["pattern"] = body["pattern"]
                if "prerelease" in body:
                    app["prerelease"] = bool(body["prerelease"])
                app["latest"] = None
                self.save()
            self.check(app)
            return {"ok": True}

        if path == "/api/check":
            targets = [self._app(body)] if body.get("id") else list(cfg["apps"])
            for a in targets:
                self.check(a)
            return {"ok": True}

        if path == "/api/update":
            if body.get("id"):
                targets = [self._app(body)]
            else:
                targets = [a for a in cfg["apps"] if has_update(a)]
            for a in targets:
                self.install(a)
            return {"ok": True, "count": len(targets)}

        if path == "/api/rollback":
            app = self._app(body)
            rollback_app(app)
            self.save()
            return {"ok": True}

        if path == "/api/remove":
            app = self._app(body)
            with _lock:
                remove_app(cfg, app, body.get("delete_files", False))
                self.save()
            return {"ok": True}

        if path == "/api/launch":
            launch_app(self._app(body))
            return {"ok": True}

        if path == "/api/open":
            app = self._app(body) if body.get("id") else None
            if app:
                p = (app.get("installed") or {}).get("path", "")
                open_path(p if os.path.isdir(p) else os.path.dirname(p))
            else:
                d = os.path.expanduser(cfg["settings"]["install_dir"])
                os.makedirs(d, exist_ok=True)
                open_path(d)
            return {"ok": True}

        if path == "/api/export":
            with _lock:
                return export_apps(cfg)

        if path == "/api/import":
            with _lock:
                added, skipped = import_apps(cfg, body.get("data"))
                self.save()
            for a in added:
                self.check(a)
            return {"ok": True, "added": len(added), "skipped": skipped}

        if path == "/api/settings":
            with _lock:
                s = cfg["settings"]
                if body.get("install_dir"):
                    s["install_dir"] = body["install_dir"].strip()
                if "keep_backup" in body:
                    s["keep_backup"] = bool(body["keep_backup"])
                if body.get("clear_token"):
                    s["github_token"] = ""
                elif body.get("github_token"):
                    s["github_token"] = body["github_token"].strip()
                self.save()
            return {"ok": True}

        raise LookupError("unknown endpoint")

    def _app(self, body):
        app = find_app(self.cfg, body.get("id", ""))
        if not app:
            raise LookupError("app not found")
        return app


def make_handler(srv, httpd_ref):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, data, ctype="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            srv.last_seen = time.time()
            if self.path in ("/", "/index.html"):
                html = INDEX_HTML.replace("__TOKEN__", srv.token).replace("__VERSION__", VERSION)
                self._send(200, html.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            srv.last_seen = time.time()
            # Token blocks other websites in the browser from driving this local server.
            if self.headers.get("X-DeckPort-Token") != srv.token:
                return self._send(403, {"error": "bad token, reload the page"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/quit":
                    self._send(200, {"ok": True})
                    threading.Thread(target=httpd_ref[0].shutdown, daemon=True).start()
                    return
                self._send(200, srv.handle(self.path, body))
            except LookupError as e:
                self._send(404, {"error": str(e)})
            except Exception as e:
                self._send(400, {"error": str(e)})

    return Handler


CHROMIUM_EXES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                 "brave", "brave-browser", "microsoft-edge", "microsoft-edge-stable")
CHROMIUM_FLATPAKS = ("com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser", "com.microsoft.Edge")


def open_window(url, window=True):
    """Show the UI. With window=True, try a Chromium-based browser's app mode (its own window,
    no tabs or address bar); Chrome from Flatpak is the usual one on a Steam Deck."""
    def spawn(cmd):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    if window and sys.platform.startswith("linux"):
        flags = ["--app=" + url, "--class=DeckPort", "--window-size=1100,760"]
        for exe in CHROMIUM_EXES:
            path = shutil.which(exe)
            if path:
                return spawn([path] + flags)
        if shutil.which("flatpak"):
            for app_id in CHROMIUM_FLATPAKS:
                if subprocess.run(["flatpak", "info", app_id], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0:
                    return spawn(["flatpak", "run", app_id] + flags)
    webbrowser.open(url)


IDLE_EXIT_SECONDS = 150  # the page polls at least once a minute, even as a background tab


def cmd_serve(args):
    url = "http://127.0.0.1:%d/" % args.port
    srv = Server()
    httpd_ref = [None]
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(srv, httpd_ref))
    except OSError:
        # Already running: just open the existing window.
        print("DeckPort is already running at", url)
        if not args.no_browser:
            open_window(url, args.window)
        return
    httpd_ref[0] = httpd
    print("DeckPort running at", url, "(Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: open_window(url, args.window)).start()
    if args.exit_when_closed:
        def watch():
            while True:
                time.sleep(10)
                if time.time() - srv.last_seen > IDLE_EXIT_SECONDS and not srv.busy():
                    print("Window closed, exiting.")
                    httpd.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Bye!")


# --------------------------------------------------------------------------- CLI

def fmt_inst(a):
    inst = a.get("installed") or {}
    return inst.get("tag") or "-"


def cmd_add(args):
    cfg = load_config()
    app = new_app(args.repo, args.name or "", args.pattern or "", args.pre)
    if any(same_source(a, app) for a in cfg["apps"]):
        sys.exit("%s is already tracked" % display_repo(app))
    cfg["apps"].append(app)
    print("Checking %s ..." % display_repo(app))
    check_app(app, cfg["settings"]["github_token"])
    save_config(cfg)
    if app["error"]:
        print("Added, but:", app["error"])
    else:
        print("Added %s, latest %s (%s)" % (app["name"], app["latest"]["tag"], app["latest"]["asset"]["name"]))
        print("Run: deckport.py update %s" % safe_name(app["name"]))


def cmd_list(args):
    cfg = load_config()
    if not cfg["apps"]:
        print("No apps tracked yet. Add one: deckport.py add https://github.com/owner/repo")
        return
    print("%-24s %-32s %-16s %-16s" % ("NAME", "REPO", "INSTALLED", "LATEST"))
    for a in cfg["apps"]:
        latest = (a.get("latest") or {}).get("tag") or "?"
        flag = "  <- update" if has_update(a) else ""
        print("%-24s %-32s %-16s %-16s%s" % (a["name"][:24], display_repo(a)[:32], fmt_inst(a)[:16], latest[:16], flag))


def cmd_check(args):
    cfg = load_config()
    n = 0
    for a in cfg["apps"]:
        upd = check_app(a, cfg["settings"]["github_token"])
        n += upd
        status = "ERROR: " + a["error"] if a["error"] else ("update available" if upd else "up to date")
        print("%-24s %s -> %s  %s" % (a["name"], fmt_inst(a), (a.get("latest") or {}).get("tag", "?"), status))
    save_config(cfg)
    print("\n%d update(s) available." % n)


def cmd_update(args):
    cfg = load_config()
    token = cfg["settings"]["github_token"]
    if args.names:
        targets = []
        for nm in args.names:
            a = find_app(cfg, nm)
            if not a:
                sys.exit("Unknown app: %s" % nm)
            targets.append(a)
        for a in targets:
            check_app(a, token)
    else:
        targets = [a for a in cfg["apps"] if check_app(a, token)]
    if not targets:
        print("Everything is up to date.")
        save_config(cfg)
        return
    for a in targets:
        if not args.names and not has_update(a):
            continue
        print("Updating %s to %s ..." % (a["name"], (a.get("latest") or {}).get("tag")))

        def prog(done, total):
            if total:
                sys.stdout.write("\r  %5.1f%%  %.1f/%.1f MB" % (done * 100 / total, done / 1e6, total / 1e6))
                sys.stdout.flush()
        try:
            path = install_app(a, cfg["settings"], prog)
            print("\n  -> %s" % path)
        except Exception as e:
            print("\n  FAILED: %s" % e)
        save_config(cfg)


def cmd_remove(args):
    cfg = load_config()
    a = find_app(cfg, args.name)
    if not a:
        sys.exit("Unknown app: %s" % args.name)
    remove_app(cfg, a, args.delete_files)
    save_config(cfg)
    print("Removed", a["name"])


def cmd_export(args):
    data = json.dumps(export_apps(load_config()), indent=2)
    if args.file in (None, "-"):
        print(data)
    else:
        Path(args.file).write_text(data)
        print("Exported to", args.file)


def cmd_import(args):
    cfg = load_config()
    try:
        data = json.loads(Path(args.file).read_text())
        added, skipped = import_apps(cfg, data)
    except (OSError, ValueError) as e:
        sys.exit("Import failed: %s" % e)
    save_config(cfg)
    print("Imported %d app(s), skipped %d already tracked." % (len(added), skipped))
    if added:
        print("Run: deckport.py check")


def main():
    p = argparse.ArgumentParser(prog="deckport", description="Update AppImages & PC ports from GitHub Releases")
    p.add_argument("--version", action="version", version="DeckPort " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="open the web UI (default)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--window", action="store_true", help="open in its own window (Chrome/Chromium app mode) if possible")
    s.add_argument("--exit-when-closed", action="store_true", help="quit a while after the page is closed")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("add", help="track a GitHub, GitLab, Codeberg, Gitea or Forgejo repo")
    s.add_argument("repo")
    s.add_argument("--name")
    s.add_argument("--pattern", help="regex to choose the release asset, e.g. 'linux.*\\.AppImage$'")
    s.add_argument("--pre", action="store_true", help="include pre-releases")
    s.set_defaults(fn=cmd_add)

    sub.add_parser("list", help="list tracked apps").set_defaults(fn=cmd_list)
    sub.add_parser("check", help="check all for updates").set_defaults(fn=cmd_check)

    s = sub.add_parser("update", help="install updates (all, or the named apps)")
    s.add_argument("names", nargs="*")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("remove", help="stop tracking an app")
    s.add_argument("name")
    s.add_argument("--delete-files", action="store_true")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("export", help="save the app list to share or back up (no tokens)")
    s.add_argument("file", nargs="?", help="output file (default: print)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("import", help="add apps from an export file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)

    args = p.parse_args()
    if not args.cmd:
        args = p.parse_args(["serve"] + sys.argv[1:])
    args.fn(args)


# --------------------------------------------------------------------------- UI

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeckPort</title>
<style>
:root{--bg:#0e141b;--panel:#17202b;--panel2:#1e2a38;--line:#2a3a4d;--text:#e6edf3;--dim:#8b9bb0;
--accent:#1a9fff;--accent2:#0d7fd6;--good:#4cc38a;--warn:#f5b041;--bad:#ef5f5f;--r:12px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0e141b 70%,#0e141bcc);padding:16px 20px;
display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0 auto 0 0;letter-spacing:.3px}
h1 small{color:var(--dim);font-weight:400;font-size:12px;margin-left:6px}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:10px;
padding:10px 16px;cursor:pointer;min-height:44px}
button:hover{border-color:var(--accent)} button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover{background:var(--accent2)}
button.danger{color:var(--bad)} button.ghost{background:transparent}
main{padding:20px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px;display:flex;flex-direction:column;gap:10px}
.card.upd{border-color:#1a9fff88;box-shadow:0 0 0 1px #1a9fff33 inset}
.top{display:flex;align-items:flex-start;gap:10px}
.name{font-weight:700;font-size:17px;word-break:break-word}
.repo{color:var(--dim);font-size:13px;text-decoration:none} .repo:hover{color:var(--accent)}
.badge{margin-left:auto;font-size:12px;padding:3px 9px;border-radius:99px;white-space:nowrap;border:1px solid}
.b-ok{color:var(--good);border-color:#4cc38a55}.b-up{color:var(--accent);border-color:#1a9fff66}
.b-new{color:var(--warn);border-color:#f5b04155}.b-err{color:var(--bad);border-color:#ef5f5f55}
.vers{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:13px}
.vers span:nth-child(odd){color:var(--dim)} .mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all}
.err{color:var(--bad);font-size:13px} .ok{color:var(--good)} .dim{color:var(--dim)}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto}
.actions button{padding:8px 12px;min-height:40px}
.bar{height:6px;background:var(--panel2);border-radius:9px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);transition:width .3s}
.jobtxt{font-size:12px;color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:60px 20px}
.empty b{display:block;color:var(--text);font-size:18px;margin-bottom:6px}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:0;width:min(560px,94vw)}
dialog::backdrop{background:#000a}
.dlg{padding:20px;display:flex;flex-direction:column;gap:12px}
.dlg h2{margin:0;font-size:18px}
label{font-size:13px;color:var(--dim);display:flex;flex-direction:column;gap:5px}
label.row{flex-direction:row;align-items:center;gap:8px;color:var(--text)}
input[type=text],input[type=password]{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;min-height:44px}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
input[type=checkbox]{width:20px;height:20px}
.assets{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:10px;font-size:13px}
.assets div{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;gap:8px;cursor:pointer}
.assets div:last-child{border:0} .assets div:hover{background:var(--panel2)}
.assets .pick{background:#1a9fff22;color:#fff} .assets .pick::before{content:"✓";color:var(--accent)}
.assets small{margin-left:auto;color:var(--dim)}
.hint{font-size:12px;color:var(--dim)}
.dlg .btns{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line);
padding:12px 18px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none;max-width:90vw}
#toast.show{opacity:1} #toast.bad{border-color:var(--bad)}
</style></head><body>
<header>
  <h1>DeckPort<small>v__VERSION__</small></h1>
  <button id="btnCheck">⟳ Check all</button>
  <button id="btnUpdAll" class="primary" disabled>Update all</button>
  <button id="btnAdd" class="primary">＋ Add app</button>
  <button id="btnSet" class="ghost" title="Settings">⚙</button>
</header>
<main><div id="list" class="grid"></div></main>

<dialog id="dAdd"><form class="dlg" method="dialog" id="fAdd">
  <h2 id="addTitle">Add app</h2>
  <label>Repo link (GitHub, GitLab, Codeberg, Gitea, Forgejo)<input type="text" id="aRepo" placeholder="https://github.com/owner/repo" required></label>
  <label>Name (optional)<input type="text" id="aName" placeholder="Shown in the list, also the file name"></label>
  <label>Asset pattern (optional regex)<input type="text" id="aPat" placeholder="auto: picks the Linux AppImage / Linux build"></label>
  <label class="row"><input type="checkbox" id="aPre"> Include pre-releases / nightlies</label>
  <div class="btns" style="justify-content:flex-start"><button type="button" id="aPrev">Preview assets</button><span class="hint" id="aInfo"></span></div>
  <div class="assets" id="aAssets" hidden></div>
  <div class="hint">Tip: click an asset to auto-fill a pattern for it.</div>
  <div class="btns"><button type="button" value="cancel" onclick="dAdd.close()">Cancel</button><button class="primary" id="aSave" type="submit">Add</button></div>
</form></dialog>

<dialog id="dSet"><form class="dlg" method="dialog" id="fSet">
  <h2>Settings</h2>
  <label>Install folder<input type="text" id="sDir"></label>
  <div class="hint">AppImages are saved with a fixed name (e.g. <span class="mono">Name.AppImage</span>) so your Steam shortcuts keep working after updates.</div>
  <label class="row"><input type="checkbox" id="sBak"> Keep previous version as <span class="mono">.old</span> (enables rollback)</label>
  <label>GitHub token (optional, raises rate limit from 60 to 5000 requests/hour)<input type="password" id="sTok" placeholder="ghp_… (leave empty to keep current)"></label>
  <div class="hint" id="sTokInfo"></div>
  <label>App list</label>
  <div class="btns" style="justify-content:flex-start;margin-top:-6px">
    <button type="button" id="sExport">Export list</button>
    <button type="button" id="sImport">Import list</button>
    <input type="file" id="sImportFile" accept=".json,application/json" hidden>
  </div>
  <div class="hint">The export contains only repos, names and asset patterns. No token, no files, no paths. Use it as a backup or to share your list.</div>
  <div class="btns" style="justify-content:space-between">
    <span><button type="button" id="sOpen">Open folder</button> <button type="button" class="danger" id="sQuit">Quit DeckPort</button></span>
    <span><button type="button" onclick="dSet.close()">Cancel</button> <button class="primary" type="submit">Save</button></span>
  </div>
</form></dialog>
<div id="toast"></div>

<script>
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S={apps:[],jobs:{},settings:{}}, editId=null, timer=null;

async function api(path,body={}){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-DeckPort-Token":TOKEN},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({error:"Bad response"}));
  if(!r.ok) throw new Error(j.error||r.statusText);
  return j;
}
function toast(msg,bad){const t=$("#toast");t.textContent=msg;t.className="show"+(bad?" bad":"");clearTimeout(t._h);t._h=setTimeout(()=>t.className="",3500)}
async function act(path,body,ok){try{const r=await api(path,body);if(ok)toast(typeof ok=="function"?ok(r):ok);refresh()}catch(e){toast(e.message,true)}}
const mb=b=>b?(b/1e6).toFixed(1)+" MB":"";
const ago=t=>{if(!t)return"never";const s=Date.now()/1000-t;if(s<60)return"just now";if(s<3600)return Math.floor(s/60)+"m ago";if(s<86400)return Math.floor(s/3600)+"h ago";return Math.floor(s/86400)+"d ago"};

function card(a){
  const job=S.jobs[a.id], inst=a.installed, L=a.latest||{};
  let badge;
  if(a.error) badge='<span class="badge b-err">Error</span>';
  else if(!inst||!a.exists) badge='<span class="badge b-new">Not installed</span>';
  else if(a.has_update) badge='<span class="badge b-up">Update</span>';
  else if(L.tag) badge='<span class="badge b-ok">Up to date</span>';
  else badge='<span class="badge">…</span>';
  const running=job&&job.state=="running";
  let jobHtml="";
  if(job){
    if(job.state=="running") jobHtml=`<div class="bar"><i style="width:${job.label=="Downloading"?Math.round(job.progress*100):100}%"></i></div><div class="jobtxt">${esc(job.label)}… ${esc(job.message)}</div>`;
    else if(job.state=="error") jobHtml=`<div class="err">${esc(job.message)}</div>`;
    else jobHtml=`<div class="jobtxt" style="color:var(--good)">✓ ${job.label=="Checking"?"Checked":"Installed"}</div>`;
  }
  const mainBtn=(!inst||!a.exists)?"Install":(a.has_update?"Update":null);
  return `<div class="card ${a.has_update&&inst?"upd":""}">
   <div class="top"><div><div class="name">${esc(a.name)}</div>
     <a class="repo" href="${esc(a.web_url)}" target="_blank" rel="noopener">${esc(a.display_repo)}</a></div>${badge}</div>
   <div class="vers">
     <span>Installed</span><span class="mono">${esc(inst?inst.tag:"-")}</span>
     <span>Latest</span><span class="mono">${L.url?`<a class="repo" href="${esc(L.url)}" target="_blank" rel="noopener">${esc(L.tag)}</a>`:"-"}${L.prerelease?" (pre)":""}</span>
     <span>Asset</span><span class="mono">${esc(L.asset?L.asset.name:"-")} ${L.asset&&L.asset.size?"· "+mb(L.asset.size):""}</span>
     <span>Checked</span><span>${ago(a.checked_at)}</span>
     ${inst&&a.exists?`<span>Integrity</span><span>${inst.verified?`<span class="ok" title="${esc(inst.sha256)}">✓ SHA-256 verified</span> <span class="dim">(${esc(inst.verified_by)})</span>`:`<span class="dim" title="${esc(inst.sha256||"")}">Not verified, no checksum published</span>`}</span>`:""}
   </div>
   ${a.error&&!job?`<div class="err">${esc(a.error)}</div>`:""}
   ${jobHtml}
   <div class="actions">
     ${mainBtn?`<button class="primary" data-a="update" data-id="${a.id}" ${running||!L.asset?"disabled":""}>${mainBtn}</button>`:""}
     <button data-a="check" data-id="${a.id}" ${running?"disabled":""}>Check</button>
     ${inst&&a.exists?`<button data-a="launch" data-id="${a.id}">${inst.kind=="archive"?"Open":"Launch"}</button>`:""}
     ${a.has_backup?`<button data-a="rollback" data-id="${a.id}" title="Restore previous version">Rollback</button>`:""}
     <button class="ghost" data-a="edit" data-id="${a.id}">Edit</button>
     <button class="ghost danger" data-a="remove" data-id="${a.id}">Remove</button>
   </div></div>`;
}

function render(){
  const L=$("#list");
  if(!S.apps.length){L.innerHTML='<div class="empty" style="grid-column:1/-1"><b>No apps yet</b>Click “Add app” and paste a GitHub link of a PC port or AppImage.</div>'}
  else L.innerHTML=S.apps.map(card).join("");
  const n=S.apps.filter(a=>a.has_update&&!(S.jobs[a.id]&&S.jobs[a.id].state=="running")).length;
  const b=$("#btnUpdAll");b.disabled=!n;b.textContent=n?`Update all (${n})`:"Update all";
}
async function refresh(){
  try{S=await api("/api/state");render()}catch(e){$("#list").innerHTML='<div class="empty" style="grid-column:1/-1"><b>DeckPort is not running</b>Start it again from the app menu.</div>'}
  clearTimeout(timer);
  const busy=Object.values(S.jobs||{}).some(j=>j.state=="running");
  timer=setTimeout(refresh,busy?700:(Object.keys(S.jobs||{}).length?2000:15000));
}

$("#list").addEventListener("click",e=>{
  const b=e.target.closest("button[data-a]");if(!b)return;
  const id=b.dataset.id,a=S.apps.find(x=>x.id==id),k=b.dataset.a;
  if(k=="update")act("/api/update",{id});
  else if(k=="check")act("/api/check",{id});
  else if(k=="launch")act("/api/launch",{id},"Launching "+a.name);
  else if(k=="rollback"){if(confirm(`Restore the previous version of ${a.name}?`))act("/api/rollback",{id},"Rolled back")}
  else if(k=="remove"){
    if(!confirm(`Stop tracking ${a.name}?`))return;
    const del=a.installed&&confirm("Also delete its downloaded files?\n\nOK = delete files, Cancel = keep files");
    act("/api/remove",{id,delete_files:!!del},"Removed");
  }
  else if(k=="edit")openAdd(a);
});
$("#btnCheck").onclick=()=>act("/api/check",{});
$("#btnUpdAll").onclick=()=>act("/api/update",{},r=>`Updating ${r.count} app(s)`);

function openAdd(a){
  editId=a?a.id:null;
  $("#addTitle").textContent=a?"Edit "+a.name:"Add app";
  $("#aSave").textContent=a?"Save":"Add";
  $("#aRepo").value=a?a.repo:"";$("#aRepo").disabled=!!a;
  $("#aName").value=a?a.name:"";$("#aPat").value=a?a.pattern||"":"";$("#aPre").checked=a?!!a.prerelease:false;
  $("#aAssets").hidden=true;$("#aInfo").textContent="";
  dAdd.showModal(); if(a)preview();
}
$("#btnAdd").onclick=()=>openAdd(null);
const reEsc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
function patternFor(name){
  // Replace version-like chunks with wildcards so the pattern keeps matching future releases.
  return "^"+reEsc(name).replace(/v?\d+(\\\.\d+)+[\w-]*?(?=[-_\\]|$)/g,".*").replace(/(\.\*)+/g,".*")+"$";
}
async function preview(){
  const box=$("#aAssets");$("#aInfo").textContent="Loading…";
  try{
    const r=await api("/api/preview",{repo:$("#aRepo").value,pattern:$("#aPat").value,prerelease:$("#aPre").checked});
    $("#aInfo").textContent=`${r.repo} · ${r.tag} · ${r.picked?"will download: "+r.picked:"⚠ no match"}`;
    box.innerHTML=r.assets.map(x=>`<div class="${x.name==r.picked?"pick":""}" data-n="${esc(x.name)}">${esc(x.name)}<small>${mb(x.size)}</small></div>`).join("")||"<div>No assets in this release</div>";
    box.hidden=false;
  }catch(e){$("#aInfo").textContent="⚠ "+e.message;box.hidden=true}
}
$("#aPrev").onclick=preview;
$("#aAssets").onclick=e=>{const d=e.target.closest("[data-n]");if(!d)return;$("#aPat").value=patternFor(d.dataset.n);preview()};
$("#fAdd").onsubmit=async e=>{
  e.preventDefault();
  const body={name:$("#aName").value,pattern:$("#aPat").value,prerelease:$("#aPre").checked};
  try{
    if(editId)await api("/api/edit",{id:editId,...body});
    else await api("/api/add",{repo:$("#aRepo").value,...body});
    dAdd.close();toast(editId?"Saved":"Added, checking…");refresh();
  }catch(err){toast(err.message,true)}
};

$("#btnSet").onclick=()=>{
  $("#sDir").value=S.settings.install_dir||"";$("#sBak").checked=!!S.settings.keep_backup;$("#sTok").value="";
  $("#sTokInfo").innerHTML=S.settings.has_token?'Token saved. <a href="#" id="clrTok" class="repo">Remove token</a>':"No token set.";
  const c=$("#clrTok");if(c)c.onclick=ev=>{ev.preventDefault();act("/api/settings",{clear_token:true},"Token removed");dSet.close()};
  dSet.showModal();
};
$("#fSet").onsubmit=e=>{e.preventDefault();act("/api/settings",{install_dir:$("#sDir").value,keep_backup:$("#sBak").checked,github_token:$("#sTok").value},"Settings saved");dSet.close()};
$("#sOpen").onclick=()=>act("/api/open",{});
$("#sExport").onclick=async()=>{
  try{
    const data=await api("/api/export");
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
    const a=document.createElement("a");a.href=url;a.download="deckport-apps-"+data.exported_at.slice(0,10)+".json";
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    toast(`Exported ${data.apps.length} app(s)`);
  }catch(e){toast(e.message,true)}
};
$("#sImport").onclick=()=>$("#sImportFile").click();
$("#sImportFile").onchange=async e=>{
  const f=e.target.files[0];e.target.value="";if(!f)return;
  try{
    if(f.size>5e6)throw new Error("File is too large");
    let data;try{data=JSON.parse(await f.text())}catch{throw new Error("That file is not valid JSON")}
    const r=await api("/api/import",{data});
    dSet.close();toast(`Imported ${r.added} app(s)`+(r.skipped?`, ${r.skipped} already tracked`:""));refresh();
  }catch(err){toast(err.message,true)}
};
$("#sQuit").onclick=async()=>{try{await api("/api/quit")}catch(e){}document.body.innerHTML='<div class="empty"><b>DeckPort closed</b>You can close this tab.</div>'};
refresh();
</script></body></html>
"""

if __name__ == "__main__":
    main()
