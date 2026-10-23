#!/usr/bin/env python3
"""
DeckPort - Obtainium-style updater for AppImages & PC ports on Steam Deck.

Track GitHub Releases, check for updates, and download/replace them in one place.
Pure Python standard library: no pip installs needed on SteamOS.

Usage:
  deckport.py serve                 # open the web UI (default)
  deckport.py add <github-url>      # track a repo
  deckport.py list
  deckport.py scan [--track]        # find AppImages you already have
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
import shlex
import shutil
import struct
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
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.5.0"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "deckport"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PORT = 8765
USER_AGENT = "DeckPort/" + VERSION

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")
# Assets that are clearly not for a Steam Deck (x86_64 Linux).
NOT_FOR_DECK = re.compile(
    r"(windows|win32|win64|[-_.]win[-_.]|\.exe$|\.msi$|macos|mac[-_.]?os|darwin|osx|\.dmg$|\.pkg$"
    r"|android|\.apk$|[-_.]ios[-_.]|aarch64|arm64|armhf|armv\d|[-_.]arm([-_.]?\d+|el)?([-_. )]|$)|i386|i686|riscv|ppc64|s390x|\.zsync$|\.sha\d*(sum)?$|\.sig$|\.asc$"
    r"|checksums?|\.blockmap$|\.txt$|\.json$|\.ya?ml$|\.deb$|\.rpm$|flatpak|\.snap$|debug|[-_.]dbg([-_.]|$)|symbols|\.pdb$)",
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
        "prerelease": bool(r.get("prerelease")), "draft": bool(r.get("draft")), "body": r.get("body") or "",
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
        "prerelease": bool(r.get("prerelease")), "draft": bool(r.get("draft")), "body": r.get("body") or "",
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
        "prerelease": bool(r.get("upcoming_release")), "draft": False, "body": r.get("description") or "",
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
    elif not n.endswith(".appimage") and re.search(r"appimage|ubuntu", n):
        s += 30  # e.g. melonDS-1.1-appimage-x86_64.zip
    if "appimage" in n and not n.endswith(".appimage"):
        s += 5  # a zipped AppImage beats a loose build of the same release
    if "sse2" in n:
        s -= 5  # builds for very old CPUs; the Deck has AVX2
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


NOTES_MAX = 6000  # release notes kept per app, enough for a "What's new" dialog


def release_info(rel, asset):
    info = {
        "tag": rel.get("tag_name") or "",
        "title": rel.get("name") or rel.get("tag_name") or "",
        "published": rel.get("published_at") or "",
        "url": rel.get("html_url") or "",
        "prerelease": bool(rel.get("prerelease")),
        "notes": (rel.get("body") or "")[:NOTES_MAX],
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


def is_skipped(app):
    """The user chose "Skip this version" for exactly the latest asset."""
    sk = app.get("skip") or {}
    la = (app.get("latest") or {}).get("asset") or {}
    return bool(sk and la) and sk.get("asset_id") == la.get("id") and sk.get("updated_at") == la.get("updated_at")


def has_update(app, honor_skip=True):
    latest = app.get("latest") or {}
    la = latest.get("asset")
    if not la:
        return False
    inst = app.get("installed")
    if not inst or not inst.get("path") or not Path(inst["path"]).exists():
        return True
    # Asset id + timestamp also catches "nightly"/"continuous" tags that get re-uploaded.
    newer = inst.get("asset_id") != la["id"] or inst.get("updated_at") != la["updated_at"]
    return newer and not (honor_skip and is_skipped(app))


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


def pinned_path(app):
    """Where this app was first installed or adopted; updates keep it so Steam shortcuts survive renames."""
    return app.get("target_path") or (app.get("installed") or {}).get("path") or None


def install_app(app, settings, progress=None, phase=None):
    latest = app.get("latest") or {}
    asset = latest.get("asset")
    if not asset:
        raise RuntimeError(app.get("error") or "Nothing to install, run a check first")

    install_dir = Path(os.path.expanduser(settings["install_dir"]))
    install_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(app["name"])
    pinned = pinned_path(app)
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
                dest_dir = Path(pinned) if pinned and os.path.isdir(pinned) else install_dir / base
                copy_over(str(root), str(dest_dir))
                path = str(dest_dir)

        if kind in ("appimage", "file"):
            suffix = ".AppImage" if kind == "appimage" else ""
            dest = install_dir / (base + suffix)
            if pinned and not os.path.isdir(pinned):
                dest = Path(pinned)  # the first install pins the path; renaming the app never moves it
            make_executable(dl)
            if dest.exists() and settings.get("keep_backup", True):
                os.replace(dest, str(dest) + ".old")
            shutil.move(str(dl), str(dest))  # same filesystem, so this is an atomic rename
            path = str(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if os.path.isdir(path):
        app.pop("target_path", None)  # target_path means a single file; folders are pinned via installed.path
    else:
        app["target_path"] = path
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
        "size": disk_size(path),
    }
    app["error"] = ""
    return path


def disk_size(path):
    """Bytes used by a file, or by a folder port (walked once at install, then stored)."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
    return total


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_STEM_NOISE = re.compile(
    r"(?<![a-z])v?\d+([._-]\d+)+[a-z0-9]*"            # versions: v1.2, 1.2.3-rc1, 2024.10.01
    r"|(?<![a-z0-9])\d{6,}(?![a-z0-9])"                 # build numbers / dates
    r"|(?<![a-z])(x86[_-]?64|amd64|x64|linux(64)?|appimage|portable|release|nightly|latest|stable)(?![a-z])")


def stem_of(name):
    """A file name without version, arch and extension: Game-v1.1-x86_64.AppImage -> "game"."""
    n = re.sub(r"\.appimage$", "", name.lower())
    return re.sub(r"[^a-z0-9]+", "", _STEM_NOISE.sub(" ", n))


def short_home(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def tracked_paths(cfg, except_id=None):
    out = set()
    for a in cfg["apps"]:
        if a["id"] == except_id:
            continue
        for p in ((a.get("installed") or {}).get("path"), a.get("target_path")):
            if p:
                out.add(os.path.realpath(p))
    return out


def find_adopt_candidates(cfg, name, repo, asset_name="", except_id=None):
    """AppImages already in the install folder that DeckPort doesn't manage yet, best match first."""
    folder = os.path.expanduser(cfg["settings"]["install_dir"])
    if not os.path.isdir(folder):
        return []
    taken = tracked_paths(cfg, except_id)
    want_asset = stem_of(asset_name) if asset_name else ""
    want_names = {t for t in (stem_of(name or ""), stem_of(repo.split("/")[-1])) if len(t) >= 3}
    out = []
    for f in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, f)
        if not f.lower().endswith(".appimage") or os.path.islink(p) or not os.path.isfile(p):
            continue
        if os.path.realpath(p) in taken:
            continue
        st = stem_of(f)
        score = 0
        if want_asset and st == want_asset:
            score = 2
        elif len(st) >= 3 and any(st == t or st.startswith(t) or t.startswith(st) for t in want_names):
            score = 1
        out.append({"path": p, "name": f, "size": os.path.getsize(p), "score": score})
    out.sort(key=lambda c: -c["score"])
    for i, c in enumerate(out):
        c["suggested"] = i == 0 and c["score"] > 0
    return out


def validate_adopt_path(cfg, path, except_id=None):
    folder = os.path.realpath(os.path.expanduser(cfg["settings"]["install_dir"]))
    p = os.path.expanduser(str(path or ""))
    if os.path.islink(p):
        raise ValueError("%s is a symlink; pick the real AppImage file" % os.path.basename(p))
    rp = os.path.realpath(p)
    if os.path.dirname(rp) != folder:
        raise ValueError("Pick an AppImage directly inside %s" % folder)
    if not rp.lower().endswith(".appimage") or not os.path.isfile(rp):
        raise ValueError("%s is not an AppImage file" % os.path.basename(rp))
    if rp in tracked_paths(cfg, except_id):
        raise ValueError("%s is already managed by another app" % os.path.basename(rp))
    return rp


def adopt_app(app, path):
    """Manage an AppImage the user already has: future updates overwrite this exact path, so
    a Steam shortcut to it keeps working. Run check_app first so the latest release is known."""
    latest = app.get("latest") or {}
    asset = latest.get("asset")
    actual = file_sha256(path)
    expected, source = expected_sha256(asset) if asset else ("", "")
    if asset and expected:
        current, verified = expected == actual, expected == actual
    else:
        current, verified = bool(asset) and os.path.basename(path) == asset["name"], False
    app["target_path"] = path
    app["installed"] = {
        "tag": latest.get("tag", "") if current else "unknown",
        "asset": os.path.basename(path),
        "asset_id": asset["id"] if current else None,  # unknown version -> has_update() offers the update
        "updated_at": asset["updated_at"] if current else "",
        "kind": "appimage",
        "path": path,
        "date": time.time(),
        "sha256": actual,
        "verified": verified,
        "verified_by": source if verified else "",
        "adopted": True,
        "size": os.path.getsize(path),
    }
    return current


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
    kept = (app.get("installed") or {}).get("path") or app.get("target_path")
    if not delete_files and kept and os.path.isfile(kept):
        # The user just chose to stop tracking it: the folder scan shouldn't offer it right back.
        ign = cfg["settings"].setdefault("scan_ignored", [])
        if kept not in ign:
            ign.append(kept)
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
        "name": name.strip() or (catalog_entry(src) or {}).get("name") or src["repo"].split("/")[-1],
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


# --------------------------------------------------------------------------- catalog & folder scan

# Popular Deck apps whose releases were checked to have a Linux build DeckPort picks correctly.
# "match" finds a file the user downloaded by hand (lower-case file name), so a scan can name it.
CATALOG = [
    {"name": "PCSX2", "about": "PS2", "src": "PCSX2/pcsx2", "match": r"pcsx2"},
    {"name": "RPCS3", "about": "PS3", "src": "RPCS3/rpcs3-binaries-linux", "match": r"^rpcs3"},
    {"name": "shadPS4", "about": "PS4", "src": "shadps4-emu/shadPS4", "match": r"shadps4"},
    {"name": "DuckStation", "about": "PS1", "src": "stenzek/duckstation", "match": r"duckstation"},
    {"name": "PPSSPP", "about": "PSP", "src": "hrydgard/ppsspp", "match": r"ppsspp"},
    {"name": "Vita3K", "about": "PS Vita", "src": "Vita3K/Vita3K", "match": r"vita3k"},
    {"name": "Cemu", "about": "Wii U", "src": "cemu-project/Cemu", "match": r"^cemu"},
    {"name": "Azahar", "about": "3DS", "src": "azahar-emu/azahar", "match": r"azahar"},
    {"name": "melonDS", "about": "DS", "src": "melonDS-emu/melonDS", "match": r"melonds"},
    {"name": "mGBA", "about": "GBA", "src": "mgba-emu/mgba", "match": r"^mgba"},
    {"name": "xemu", "about": "Xbox", "src": "xemu-project/xemu", "match": r"^xemu"},
    {"name": "Flycast", "about": "Dreamcast", "src": "flyinghead/flycast", "match": r"^flycast"},
    {"name": "ES-DE", "about": "Frontend", "src": "https://gitlab.com/es-de/emulationstation-de", "match": r"^es-de|emulationstation"},
    {"name": "Heroic", "about": "Epic/GOG launcher", "src": "Heroic-Games-Launcher/HeroicGamesLauncher", "match": r"^heroic"},
    {"name": "Ship of Harkinian", "about": "Ocarina of Time port", "src": "HarbourMasters/Shipwright", "match": r"^soh[-_.]|shipwright"},
    {"name": "2 Ship 2 Harkinian", "about": "Majora's Mask port", "src": "HarbourMasters/2ship2harkinian", "match": r"^2ship"},
    {"name": "Zelda 64: Recompiled", "about": "Majora's Mask recomp", "src": "Zelda64Recomp/Zelda64Recomp", "match": r"zelda64recomp"},
]


# What the Add dialog needs; "key" (host/repo) tells it which entries are already tracked.
CATALOG_UI = [dict(c, key=("%s/%s" % parse_source(c["src"])[1:]).lower()) for c in CATALOG]


def _src_key(provider, host, repo):
    return (provider or "", (host or "").lower(), (repo or "").lower())


def catalog_entry(src):
    """The catalog entry for a {provider, host, repo} source, if any."""
    want = _src_key(src.get("provider"), src.get("host"), src.get("repo"))
    for c in CATALOG:
        if _src_key(*parse_source(c["src"])) == want:
            return c
    return None


def read_update_info(path):
    """The AppImage's embedded update information (ELF section .upd_info), e.g.
    "gh-releases-zsync|owner|repo|latest|*.zsync". Read with plain file I/O: never runs the file."""
    try:
        with open(path, "rb") as f:
            h = f.read(64)
            if len(h) < 64 or h[:4] != b"\x7fELF" or h[4] != 2 or h[5] != 1:
                return ""
            shoff, = struct.unpack_from("<Q", h, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from("<HHH", h, 0x3A)
            if not shoff or shentsize < 64 or not 0 < shnum <= 1024 or shstrndx >= shnum:
                return ""
            f.seek(shoff)
            table = f.read(shentsize * shnum)
            if len(table) < shentsize * shnum:
                return ""
            secs = [struct.unpack_from("<IIQQQQ", table, i * shentsize) for i in range(shnum)]
            f.seek(secs[shstrndx][4])
            names = f.read(min(secs[shstrndx][5], 65536))
            for name_off, _type, _flags, _addr, off, size in secs:
                if names[name_off:name_off + 10] == b".upd_info\0":
                    f.seek(off)
                    return f.read(min(size, 1024)).split(b"\0")[0].decode("utf-8", "replace").strip()
    except (OSError, struct.error):
        pass
    return ""


def source_from_update_info(info):
    parts = info.split("|")
    if len(parts) >= 3 and parts[0] in ("gh-releases-zsync", "gh-releases-direct") and valid_repo(parts[1] + "/" + parts[2]):
        return parts[1] + "/" + parts[2]
    return ""


_ident_cache = {}


def identify_file(path):
    """(source text, catalog entry or None, how) for an AppImage on disk; how is "embedded",
    "catalog" or "" when DeckPort can't tell which repo it came from."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _ident_cache:
        src = source_from_update_info(read_update_info(path))
        if src:
            entry = catalog_entry(dict(zip(("provider", "host", "repo"), parse_source(src))))
            result = (src, entry, "embedded")
        else:
            low = os.path.basename(path).lower()
            entry = next((c for c in CATALOG if re.search(c["match"], low)), None)
            result = (entry["src"], entry, "catalog") if entry else ("", None, "")
        _ident_cache[key] = result
    return _ident_cache[key]


def scan_folder(cfg):
    """AppImages in the install folder that DeckPort doesn't manage yet, with the repo each one
    probably came from. A file whose repo is tracked but not installed is offered for that app."""
    folder = os.path.expanduser(cfg["settings"]["install_dir"])
    if not os.path.isdir(folder):
        return []
    taken = tracked_paths(cfg)
    ignored = set(cfg["settings"].get("scan_ignored") or [])
    by_src = {}
    for a in cfg["apps"]:
        by_src.setdefault(_src_key(a.get("provider"), a.get("host"), a["repo"]), a)
    out = []
    for f in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, f)
        if (not f.lower().endswith(".appimage") or f.lower().startswith("deckport") or os.path.islink(p)
                or not os.path.isfile(p) or os.path.realpath(p) in taken or p in ignored):
            continue
        try:
            src, entry, how = identify_file(p)
        except OSError:
            continue
        item = {"path": p, "file": f, "size": os.path.getsize(p), "src": src, "how": how,
                "name": entry["name"] if entry else "", "about": entry["about"] if entry else ""}
        if src:
            prov, host, repo = parse_source(src)
            item["display"] = repo if host == "github.com" else "%s/%s" % (host, repo)
            item["name"] = item["name"] or repo.split("/")[-1]
            owner = by_src.get(_src_key(prov or "", host, repo))
            if owner:
                inst = owner.get("installed") or {}
                if inst.get("path") and os.path.exists(inst["path"]):
                    continue  # a spare copy of an app DeckPort already keeps up to date
                item["app_id"], item["name"] = owner["id"], owner["name"]
        out.append(item)
    return out


# --------------------------------------------------------------------------- Steam shortcuts

STEAM_ROOTS = ("~/.steam/steam", "~/.local/share/Steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")


def steam_shortcuts_file():
    """shortcuts.vdf of the Steam account used most recently, or None when Steam isn't set up."""
    for root in STEAM_ROOTS:
        ud = Path(os.path.expanduser(root)) / "userdata"
        if not ud.is_dir():
            continue
        best = None
        for d in ud.iterdir():
            if not d.name.isdigit() or d.name == "0":
                continue
            lc = d / "config" / "localconfig.vdf"
            m = lc.stat().st_mtime if lc.exists() else 0
            if best is None or m > best[0]:
                best = (m, d)
        if best:
            return best[1] / "config" / "shortcuts.vdf"
    return None


_running_cache = {"at": 0.0, "value": False}


def steam_running(max_age=0.0):
    """Steam rewrites shortcuts.vdf from memory when it exits, so it must not be running.
    max_age lets the UI's frequent polls reuse a recent answer instead of walking /proc each time."""
    if time.time() - _running_cache["at"] < max_age:
        return _running_cache["value"]
    found = False
    if os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if Path("/proc", pid, "comm").read_text().strip() == "steam":
                        found = True
                        break
                except OSError:
                    pass
    _running_cache.update(at=time.time(), value=found)
    return found


# Binary KeyValues as used by shortcuts.vdf: a list of (type, key, value); type 0 is a nested list.
def vdf_load(data):
    pos = 0

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        s, pos = data[pos:end].decode("utf-8", "surrogateescape"), end + 1
        return s

    def node():
        nonlocal pos
        items = []
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == 8:
                return items
            k = cstr()
            if t == 0:
                items.append((0, k, node()))
            elif t == 1:
                items.append((1, k, cstr()))
            elif t == 2:
                items.append((2, k, struct.unpack_from("<i", data, pos)[0]))
                pos += 4
            elif t == 7:
                items.append((7, k, struct.unpack_from("<Q", data, pos)[0]))
                pos += 8
            else:
                raise ValueError("Unsupported value type %d in shortcuts.vdf" % t)
        return items

    return node()


def vdf_dump(items):
    out = bytearray()
    for t, k, v in items:
        out.append(t)
        out += k.encode("utf-8", "surrogateescape") + b"\0"
        if t == 0:
            out += vdf_dump(v)
        elif t == 1:
            out += v.encode("utf-8", "surrogateescape") + b"\0"
        elif t == 2:
            out += struct.pack("<i", v)
        elif t == 7:
            out += struct.pack("<Q", v)
    out.append(8)
    return bytes(out)


def _shortcut_list(items):
    for t, k, v in items:
        if t == 0 and k.lower() == "shortcuts":
            return v
    lst = []
    items.append((0, "shortcuts", lst))
    return lst


def _shortcut_exe(entry):
    for t, k, v in entry:
        if t == 1 and k.lower() == "exe":
            return v.strip().strip('"')
    return ""


_steam_cache = {}


def steam_shortcut_exes():
    """Executables of the current Non-Steam shortcuts (cached on the file's mtime)."""
    f = steam_shortcuts_file()
    try:
        m = f.stat().st_mtime_ns if f else None
    except OSError:
        m = None
    if m is None:
        return set()
    if _steam_cache.get("key") != (str(f), m):
        try:
            exes = {_shortcut_exe(e) for t, _k, e in _shortcut_list(vdf_load(f.read_bytes())) if t == 0}
        except (OSError, ValueError, IndexError, struct.error):
            exes = set()
        _steam_cache.update(key=(str(f), m), exes=exes)
    return _steam_cache["exes"]


def add_to_steam(app, shortcuts=None):
    """Add the installed AppImage as a Non-Steam game. Returns False if it was already there."""
    path = (app.get("installed") or {}).get("path") or ""
    if not path or not os.path.isfile(path):
        raise RuntimeError("Install %s first" % app["name"])
    f = shortcuts or steam_shortcuts_file()
    if not f:
        raise RuntimeError("Steam not found. Start Steam once and log in, then try again.")
    if shortcuts is None and steam_running():
        raise RuntimeError("Close Steam first (Steam menu > Exit), then press Add to Steam again. "
                           "In Game Mode, use the DeckPort Decky plugin instead.")
    f = Path(f)
    data = f.read_bytes() if f.exists() else b""
    root = vdf_load(data) if data else []
    lst = _shortcut_list(root)
    if any(_shortcut_exe(e) == path for t, _k, e in lst if t == 0):
        return False
    exe, name = '"%s"' % path, app["name"]
    appid = (zlib.crc32((exe + name).encode()) | 0x80000000) - (1 << 32)  # stored as a signed int32
    nxt = max([int(k) for _t, k, _v in lst if k.isdigit()] + [-1]) + 1
    lst.append((0, str(nxt), [
        (2, "appid", appid), (1, "AppName", name), (1, "Exe", exe),
        (1, "StartDir", '"%s"' % os.path.dirname(path)), (1, "icon", ""), (1, "ShortcutPath", ""),
        (1, "LaunchOptions", ""), (2, "IsHidden", 0), (2, "AllowDesktopConfig", 1), (2, "AllowOverlay", 1),
        (2, "OpenVR", 0), (2, "Devkit", 0), (1, "DevkitGameID", ""), (2, "DevkitOverrideAppID", 0),
        (2, "LastPlayTime", 0), (1, "FlatpakAppID", ""), (0, "tags", []),
    ]))
    f.parent.mkdir(parents=True, exist_ok=True)
    if data:
        shutil.copy2(f, str(f) + ".deckport.bak")  # the previous version, in case Steam dislikes ours
    tmp = f.with_suffix(".vdf.tmp")
    tmp.write_bytes(vdf_dump(root))
    os.replace(tmp, f)
    return True


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
            in_steam = steam_shortcut_exes()
            apps = []
            for a in self.cfg["apps"]:
                d = dict(a)
                d["has_update"] = has_update(a)
                d["web_url"] = web_url(a)
                d["display_repo"] = display_repo(a)
                inst = a.get("installed") or {}
                p = inst.get("path") or ""
                d["exists"] = bool(p and os.path.exists(p))
                d["has_backup"] = bool(p and os.path.isfile(p + ".old"))
                d["file_name"] = os.path.basename(p or a.get("target_path") or "")
                d["skipped"] = d["exists"] and is_skipped(a) and has_update(a, honor_skip=False)
                d["in_steam"] = bool(p) and p in in_steam
                d["disk"] = (os.path.getsize(p) if os.path.isfile(p) else inst.get("size", 0)) if d["exists"] else 0
                d["old_disk"] = os.path.getsize(p + ".old") if d["has_backup"] else 0
                apps.append(d)
            s = dict(self.cfg["settings"])
            s["has_token"] = bool(s.pop("github_token", ""))
            s["scan_ignored"] = len(s.get("scan_ignored") or [])
            s["install_dir_short"] = short_home(os.path.expanduser(s["install_dir"]))
            return {"apps": apps, "settings": s, "jobs": self.jobs.snapshot(), "version": VERSION,
                    "found": scan_folder(self.cfg), "catalog": CATALOG_UI, "steam": bool(steam_shortcuts_file()),
                    "steam_running": steam_running(max_age=10)}

    def check(self, app):
        def fn(job):
            check_app(app, self.gh_token())
            self.save()
            if app.get("error"):
                raise RuntimeError(app["error"])
        return self.jobs.start(app["id"], "Checking", fn)

    def adopt(self, app, path):
        def fn(job):
            check_app(app, self.gh_token())
            if app.get("error"):
                raise RuntimeError(app["error"])
            adopt_app(app, path)
            self.save()
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

        if path == "/api/track_found":
            # Files from the folder scan: each becomes a tracked app that updates that exact file.
            # Everything is validated first, so one bad item changes nothing.
            plan, seen = [], set()
            with _lock:
                for item in body.get("items") or []:
                    path_ = validate_adopt_path(cfg, item.get("path"), item.get("app_id"))
                    if path_ in seen:
                        raise ValueError("%s is listed twice" % os.path.basename(path_))
                    seen.add(path_)
                    if item.get("app_id"):
                        app = self._app({"id": item["app_id"]})
                    else:
                        app = new_app("", item.get("name", ""), src=resolve_source(item.get("src", "")))
                        if any(same_source(a, app) for a in cfg["apps"]) or any(same_source(a, app) for a, _p in plan):
                            raise ValueError("%s is already tracked" % display_repo(app))
                    plan.append((app, path_))
                for app, path_ in plan:
                    if app not in cfg["apps"]:
                        cfg["apps"].append(app)
                    app["target_path"] = path_
                self.save()
            for app, path_ in plan:
                self.adopt(app, path_)
            return {"ok": True, "count": len(plan)}

        if path == "/api/scan_ignore":
            with _lock:
                s = cfg["settings"]
                if body.get("clear"):
                    s["scan_ignored"] = []
                else:
                    keep = [p for p in (s.get("scan_ignored") or []) if os.path.exists(p)]
                    s["scan_ignored"] = (keep + [p for p in body.get("paths") or [] if isinstance(p, str) and p not in keep])[-200:]
                self.save()
            return {"ok": True}

        if path == "/api/add":
            app = new_app(body.get("repo", ""), body.get("name", ""), body.get("pattern", ""),
                          body.get("prerelease", False))
            with _lock:
                if any(same_source(a, app) for a in cfg["apps"]):
                    raise ValueError("%s is already tracked" % display_repo(app))
                adopt = validate_adopt_path(cfg, body["adopt_path"]) if body.get("adopt_path") else None
                if adopt:
                    app["target_path"] = adopt  # claim the file now so the folder scan stops offering it
                cfg["apps"].append(app)
                self.save()
            if adopt:
                self.adopt(app, adopt)
            else:
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
                adopt = validate_adopt_path(cfg, body["adopt_path"], app["id"]) if body.get("adopt_path") else None
                if adopt:
                    app["target_path"] = adopt
                app["latest"] = None
                self.save()
            if adopt:
                self.adopt(app, adopt)
            else:
                self.check(app)
            return {"ok": True}

        if path == "/api/check":
            targets = [self._app(body)] if body.get("id") else list(cfg["apps"])
            for a in targets:
                self.check(a)
            return {"ok": True}

        if path == "/api/update":
            if body.get("id"):
                app = self._app(body)
                inst = app.get("installed") or {}
                if inst.get("path") and os.path.exists(inst["path"]) and not has_update(app, honor_skip=False):
                    # Installed and current: don't download the same release again.
                    raise ValueError("%s is already up to date" % app["name"])
                app.pop("skip", None)  # updating on purpose undoes "Skip this version"
                targets = [app]
            else:
                targets = [a for a in cfg["apps"] if has_update(a)]
            for a in targets:
                self.install(a)
            return {"ok": True, "count": len(targets)}

        if path == "/api/skip":
            app = self._app(body)
            with _lock:
                la = (app.get("latest") or {}).get("asset")
                if body.get("undo") or not la:
                    app.pop("skip", None)
                else:
                    app["skip"] = {"asset_id": la["id"], "updated_at": la["updated_at"], "tag": app["latest"].get("tag", "")}
                self.save()
            return {"ok": True}

        if path == "/api/steam_add":
            app = self._app(body)
            added = add_to_steam(app)
            return {"ok": True, "added": added}

        if path == "/api/clean_backups":
            removed = freed = 0
            with _lock:
                for a in cfg["apps"]:
                    p = (a.get("installed") or {}).get("path") or ""
                    if p and os.path.isfile(p + ".old"):
                        freed += os.path.getsize(p + ".old")
                        os.remove(p + ".old")
                        removed += 1
            return {"ok": True, "removed": removed, "freed": freed}

        if path == "/api/rollback":
            app = self._app(body)
            if (self.jobs.snapshot().get(app["id"]) or {}).get("state") == "running":
                raise ValueError("Wait for %s to finish first" % app["name"])
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
            d = os.path.expanduser(cfg["settings"]["install_dir"])
            os.makedirs(d, exist_ok=True)
            open_path(d)
            return {"ok": True}

        if path == "/api/export":
            with _lock:
                return export_apps(cfg)

        if path == "/api/export_save":
            # The desktop app's webview can't save blob downloads, so write the file here.
            with _lock:
                data = export_apps(cfg)
            folder = Path.home() / "Downloads"
            folder.mkdir(parents=True, exist_ok=True)
            stem = "deckport-apps-" + data["exported_at"][:10]
            dest, n = folder / (stem + ".json"), 2
            while dest.exists():
                dest, n = folder / ("%s-%d.json" % (stem, n)), n + 1
            dest.write_text(json.dumps(data, indent=2))
            return {"ok": True, "path": str(dest), "count": len(data["apps"])}

        if path == "/api/open_url":
            url = body.get("url", "")
            if not isinstance(url, str) or not re.match(r"^https://[^\s/]+", url):
                raise ValueError("Only https:// links can be opened")
            open_path(url)
            return {"ok": True}

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
            if urllib.parse.urlsplit(self.path).path in ("/", "/index.html"):
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
        url = "http://127.0.0.1:%d/" % httpd.server_address[1]  # real port when --port 0
    except OSError:
        # Already running: just open the existing window.
        print("DeckPort is already running at", url)
        if not args.no_browser:
            open_window(url, args.window)
        return
    httpd_ref[0] = httpd
    # The desktop app (Tauri) waits for this exact line to learn the URL.
    print("DECKPORT_READY " + url, flush=True)
    print("DeckPort running at", url, "(Ctrl+C to stop)", flush=True)
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
    if args.exit_on_stdin_close:
        def wait_parent():
            # The desktop app holds our stdin open; EOF means its window closed (or it crashed).
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            while srv.busy():  # let a running download finish instead of leaving a half file
                time.sleep(1)
            httpd.shutdown()
        threading.Thread(target=wait_parent, daemon=True).start()
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
    try:
        adopt = validate_adopt_path(cfg, args.adopt) if args.adopt else None
    except ValueError as e:
        sys.exit("Can't adopt: %s" % e)
    cfg["apps"].append(app)
    print("Checking %s ..." % display_repo(app))
    check_app(app, cfg["settings"]["github_token"])
    if app["error"]:
        save_config(cfg)
        print("Added, but:", app["error"])
        return
    asset = app["latest"]["asset"]["name"]
    print("Added %s, latest %s (%s)" % (app["name"], app["latest"]["tag"], asset))
    if adopt:
        current = adopt_app(app, adopt)
        save_config(cfg)
        if current:
            print("Using your existing %s, already the latest version." % os.path.basename(adopt))
        else:
            print("Using your existing %s. Updates will overwrite it in place." % os.path.basename(adopt))
            print("Run: deckport.py update %s" % safe_name(app["name"]))
        return
    save_config(cfg)
    found = [c for c in find_adopt_candidates(cfg, app["name"], app["repo"], asset, app["id"]) if c["suggested"]]
    if found:
        print("You already have %s. To update that file instead of creating %s.AppImage:"
              % (found[0]["name"], safe_name(app["name"])))
        print("  deckport.py remove %s && deckport.py add %s --adopt %s"
              % (safe_name(app["name"]), args.repo, shlex.quote(found[0]["path"])))
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


def cmd_scan(args):
    cfg = load_config()
    found = scan_folder(cfg)
    if not found:
        print("No untracked AppImages in", short_home(os.path.expanduser(cfg["settings"]["install_dir"])))
        return
    for it in found:
        what = ("%s (%s)" % (it["name"], it["display"])) if it["src"] else "not recognized"
        print("%-48s %s" % (it["file"][:48], what))
    known = [it for it in found if it["src"]]
    if not args.track:
        if known:
            print("\nRun: deckport.py scan --track   to keep the %d recognized file(s) up to date" % len(known))
        return
    token = cfg["settings"]["github_token"]
    for it in known:
        if it.get("app_id"):
            app = find_app(cfg, it["app_id"])
        else:
            src = resolve_source(it["src"])
            app = new_app("", it["name"], src=src)
            cfg["apps"].append(app)
        check_app(app, token)
        if app["error"]:
            print("%s: %s" % (app["name"], app["error"]))
            continue
        current = adopt_app(app, it["path"])
        print("%s: tracking %s%s" % (app["name"], it["file"], ", up to date" if current else ", update available"))
    save_config(cfg)


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
    s.add_argument("--exit-on-stdin-close", action="store_true", help=argparse.SUPPRESS)  # used by the desktop app
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("add", help="track a GitHub, GitLab, Codeberg, Gitea or Forgejo repo")
    s.add_argument("repo")
    s.add_argument("--name")
    s.add_argument("--pattern", help="regex to choose the release asset, e.g. 'linux.*\\.AppImage$'")
    s.add_argument("--pre", action="store_true", help="include pre-releases")
    s.add_argument("--adopt", metavar="FILE", help="update this AppImage you already have (keeps its name and Steam shortcut)")
    s.set_defaults(fn=cmd_add)

    sub.add_parser("list", help="list tracked apps").set_defaults(fn=cmd_list)
    s = sub.add_parser("scan", help="find AppImages you already have that DeckPort can update")
    s.add_argument("--track", action="store_true", help="track every recognized file (updates keep its name)")
    s.set_defaults(fn=cmd_scan)
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
<meta name="color-scheme" content="dark">
<title>DeckPort</title>
<style>
:root{
  --blue-50:#eff6ff;--blue-100:#dbeafe;--blue-200:#bfdbfe;--blue-300:#93c5fd;--blue-400:#60a5fa;--blue-500:#3b82f6;
  --blue-600:#2563eb;--blue-700:#1d4ed8;--blue-800:#1e40af;--blue-900:#1e3a8a;--blue-950:#172554;
  --bg:#050814;--text:var(--blue-50);--dim:rgba(147,197,253,.72);--faint:rgba(147,197,253,.5);
  --glass:rgba(23,37,84,.38);--glass-hi:rgba(59,130,246,.10);--edge:rgba(147,197,253,.14);--edge-hi:rgba(147,197,253,.30);
  --shine:rgba(239,246,255,.08);
  /* status colours stay semantic, muted to sit on blue */
  --good:#86efac;--warn:#fcd34d;--bad:#fca5a5;
  --r:20px;--r-sm:12px;--blur:blur(20px) saturate(140%);
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;color:var(--text);background:var(--bg);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif;-webkit-font-smoothing:antialiased}
/* Static ambient light behind the glass: painted once, never animated. */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(50% 45% at 8% -8%,rgba(37,99,235,.42),transparent 72%),
    radial-gradient(40% 38% at 96% 6%,rgba(59,130,246,.22),transparent 72%),
    radial-gradient(65% 55% at 60% 112%,rgba(30,64,175,.50),transparent 72%),
    radial-gradient(35% 30% at 25% 70%,rgba(23,37,84,.55),transparent 75%)}
a{color:var(--blue-300);text-decoration:none} a:hover{color:var(--blue-200)}
.mono{font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;font-size:12.5px;word-break:break-all}
/* `hidden` must win over the display:flex on label/.adopt etc. */
[hidden]{display:none!important}

/* ---- glass surfaces: cards use layered gradients only; blur is reserved for header, dialogs, toast */
.glass{position:relative;background:linear-gradient(180deg,var(--glass-hi),rgba(30,58,138,.04)),var(--glass);
  border:1px solid var(--edge);border-radius:var(--r);
  box-shadow:inset 0 1px 0 var(--shine),0 12px 32px -16px rgba(0,0,0,.7)}
.glass::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:linear-gradient(135deg,rgba(239,246,255,.09),rgba(239,246,255,0) 38%)}
.frost{background:rgba(10,18,44,.62);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur)}

header{position:sticky;top:0;z-index:5;display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding:14px 22px;border-bottom:1px solid var(--edge);box-shadow:inset 0 -1px 0 rgba(239,246,255,.03)}
.brand{display:flex;align-items:center;gap:12px;margin-right:auto;min-width:0}
.brand svg{width:34px;height:34px;flex:none;filter:drop-shadow(0 4px 10px rgba(37,99,235,.45))}
.brand h1{font-size:19px;margin:0;font-weight:700;letter-spacing:.2px}
.brand .sub{font-size:12px;color:var(--dim)}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--edge);color:var(--blue-300);
  background:rgba(59,130,246,.10);margin-left:6px;vertical-align:2px;font-weight:500}

/* ---- buttons */
button{font:inherit;font-size:14px;font-weight:500;color:var(--blue-100);cursor:pointer;min-height:42px;padding:9px 16px;
  border-radius:var(--r-sm);border:1px solid var(--edge);background:rgba(147,197,253,.07);
  box-shadow:inset 0 1px 0 var(--shine);transition:background .15s,border-color .15s,transform .15s,box-shadow .15s}
button:hover:not(:disabled){background:rgba(147,197,253,.13);border-color:var(--edge-hi)}
button:active:not(:disabled){transform:scale(.97)}
button:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid var(--blue-400);outline-offset:2px}
button:disabled{opacity:.42;cursor:default}
button.primary{color:#fff;border-color:rgba(191,219,254,.38);
  background:linear-gradient(180deg,var(--blue-500),var(--blue-600));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.28),0 8px 20px -8px rgba(37,99,235,.8)}
button.primary:hover:not(:disabled){background:linear-gradient(180deg,#4b8ef8,var(--blue-500));border-color:rgba(219,234,254,.5)}
button.ghost{background:transparent;border-color:transparent;box-shadow:none}
button.ghost:hover:not(:disabled){background:rgba(147,197,253,.09);border-color:var(--edge)}
button.danger{color:var(--bad)}
button.icon{width:42px;padding:0;display:grid;place-items:center}
button.icon svg{width:19px;height:19px}

/* ---- layout */
main{padding:22px;max-width:1180px;margin:0 auto}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 16px;color:var(--dim);font-size:13px}
.summary span{padding:5px 12px;border-radius:99px;border:1px solid var(--edge);background:rgba(23,37,84,.35)}
.summary b{color:var(--text);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px;align-items:start}

/* ---- app cards */
.card{padding:18px;display:flex;flex-direction:column;gap:12px}
.top{display:flex;align-items:flex-start;gap:12px}
.avatar{width:40px;height:40px;flex:none;border-radius:12px;display:grid;place-items:center;font-weight:700;font-size:16px;
  color:var(--blue-50);background:linear-gradient(160deg,var(--blue-600),var(--blue-900));
  border:1px solid rgba(191,219,254,.22);box-shadow:inset 0 1px 0 rgba(255,255,255,.2)}
.name{font-weight:650;font-size:16.5px;word-break:break-word;line-height:1.25}
.repo{color:var(--dim);font-size:12.5px}
.badge{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:4px 10px;border-radius:99px;
  white-space:nowrap;border:1px solid var(--edge);background:rgba(5,8,20,.35);color:var(--blue-200)}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor}
.b-ok{color:var(--good)} .b-up{color:var(--blue-300);border-color:rgba(96,165,250,.45)} .b-new{color:var(--warn)} .b-err{color:var(--bad)}
/* one line for the version, then small tags; details live in the ⋯ menu and Edit */
.ver{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;font-size:13.5px;color:var(--blue-100)}
.ver .mono{font-size:13px} .ver b{color:#fff;font-weight:600}
.ver a[data-a]{white-space:nowrap;font-size:13px}
.tag{font-size:11px;padding:1px 7px;border-radius:99px;color:var(--blue-200);border:1px solid var(--edge);white-space:nowrap}
.tag.ok,.tag.steam{color:var(--good);border-color:rgba(134,239,172,.35)}
.fname{color:var(--faint);font-size:12px;margin-top:-6px}
.ok{color:var(--good)} .dim{color:var(--faint)} .err{color:var(--bad);font-size:13px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto}
.actions{gap:6px}
.actions button{min-height:38px;padding:7px 12px;font-size:13.5px}
.actions button.ghost{padding:7px 9px}
.actions .more{margin-left:auto;width:38px;min-height:38px}
.bar{height:6px;border-radius:99px;overflow:hidden;background:rgba(147,197,253,.12)}
.bar i{display:block;height:100%;width:100%;transform-origin:left;border-radius:inherit;
  background:linear-gradient(90deg,var(--blue-600),var(--blue-400));transition:transform .3s ease-out}
.bar.wait i{opacity:.45}
.jobtxt{font-size:12.5px;color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:56px 24px}
.empty b{display:block;color:var(--text);font-size:18px;margin:10px 0 6px}
.empty svg{width:56px;height:56px;opacity:.9}

/* ---- dialogs & toast (the only other blurred surfaces) */
dialog{color:var(--text);padding:0;width:min(580px,94vw);max-height:calc(100vh - 32px);overflow:auto;overscroll-behavior:contain;border-radius:24px;border:1px solid var(--edge-hi);
  background:rgba(13,24,58,.72);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  box-shadow:inset 0 1px 0 rgba(239,246,255,.10),0 30px 80px -20px rgba(0,0,0,.8)}
dialog::backdrop{background:rgba(5,8,20,.62)}
.dlg{padding:22px;display:flex;flex-direction:column;gap:13px}
.dlg h2{margin:0 0 2px;font-size:18px;font-weight:650}
.dlg p{margin:0;color:var(--dim)}
label{font-size:13px;color:var(--dim);display:flex;flex-direction:column;gap:6px}
label.row{flex-direction:row;align-items:center;gap:10px;color:var(--text)}
input[type=text],input[type=password]{font:inherit;color:var(--text);min-height:44px;padding:10px 13px;border-radius:var(--r-sm);
  background:rgba(5,8,20,.55);border:1px solid rgba(147,197,253,.18);box-shadow:inset 0 1px 2px rgba(0,0,0,.35);
  transition:border-color .15s,box-shadow .15s}
input[type=text]:focus,input[type=password]:focus{outline:none;border-color:var(--blue-400);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.35),0 0 0 3px rgba(59,130,246,.25)}
input::placeholder{color:rgba(147,197,253,.38)}
input[type=checkbox]{width:20px;height:20px;accent-color:var(--blue-500)}
.assets{max-height:168px;overflow:auto;border-radius:14px;font-size:13px;background:rgba(5,8,20,.4);border:1px solid var(--edge)}
.assets div{padding:8px 12px;border-bottom:1px solid rgba(147,197,253,.07);display:flex;gap:8px;cursor:pointer;transition:background .15s}
.assets div:last-child{border:0} .assets div:hover{background:rgba(59,130,246,.12)}
.assets .pick{background:rgba(59,130,246,.22);color:#fff} .assets .pick::before{content:"✓";color:var(--blue-300)}
.assets small{margin-left:auto;color:var(--faint)}
.hint{font-size:12.5px;color:var(--faint)}
.dlg .btns{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
/* The last button row stays on screen while a tall dialog scrolls (Deck screens are only 800px high). */
.dlg>.btns:last-child{position:sticky;bottom:0;z-index:1;margin:-8px -22px -22px;padding:18px 22px 22px;
  background:linear-gradient(rgba(11,20,50,0),rgba(11,20,50,.96) 16px)}
.sep{height:1px;background:var(--edge);margin:4px 0}
#toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,8px);padding:12px 18px;border-radius:14px;
  border:1px solid var(--edge-hi);background:rgba(13,24,58,.78);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  box-shadow:inset 0 1px 0 var(--shine),0 16px 40px -12px rgba(0,0,0,.7);
  opacity:0;transition:opacity .2s,transform .2s;pointer-events:none;max-width:90vw;z-index:20}
#toast.show{opacity:1;transform:translate(-50%,0)} #toast.bad{border-color:rgba(252,165,165,.5)}

/* ---- status: icon + colour per state, so "needs update" vs "up to date" reads at a glance */
.card{--st:var(--blue-300);--st-bg:rgba(147,197,253,.10);--st-edge:rgba(147,197,253,.32)}
.card.st-update{--st:var(--blue-300);--st-bg:rgba(59,130,246,.22);--st-edge:rgba(96,165,250,.6)}
.card.st-ok{--st:var(--good);--st-bg:rgba(134,239,172,.10);--st-edge:rgba(134,239,172,.35)}
.card.st-new{--st:var(--warn);--st-bg:rgba(252,211,77,.10);--st-edge:rgba(252,211,77,.35)}
.card.st-err{--st:var(--bad);--st-bg:rgba(252,165,165,.10);--st-edge:rgba(252,165,165,.38)}
.card.st-update::after{content:"";position:absolute;left:-1px;top:16px;bottom:16px;width:3px;border-radius:0 3px 3px 0;
  background:linear-gradient(180deg,var(--blue-300),var(--blue-600));box-shadow:0 0 12px rgba(96,165,250,.7)}
.badge svg{width:14px;height:14px;flex:none}
.badge::before{display:none}
.badge{color:var(--st);border-color:var(--st-edge);background:var(--st-bg)}
button svg{width:16px;height:16px;vertical-align:-3px}
.card.st-update .badge{font-weight:600;position:relative}
/* the update badge pulses three times when it first appears, then stays still */
.card.st-update .badge.pulse::after{content:"";position:absolute;inset:-1px;border-radius:inherit;
  border:1px solid var(--blue-400);animation:ring 1.5s ease-out .35s 3;opacity:0}
@keyframes ring{0%{transform:scale(1);opacity:.8}100%{transform:scale(1.35);opacity:0}}
.spin{animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* ---- filter chips */
.summary button{min-height:32px;padding:4px 12px;border-radius:99px;font-size:13px;color:var(--dim);
  background:rgba(23,37,84,.35);box-shadow:none;display:inline-flex;gap:6px;align-items:center}
.summary button b{color:var(--text);font-weight:600}
.summary button.on{color:#fff;border-color:rgba(96,165,250,.6);background:rgba(37,99,235,.32)}
.summary button i{width:7px;height:7px;border-radius:50%;background:var(--c,var(--blue-300))}

/* ---- motion: transform/opacity only, each plays once */
.enter{animation:enter .38s cubic-bezier(.2,.8,.2,1) both}
@keyframes enter{from{opacity:0;transform:translateY(8px) scale(.985)}to{opacity:1;transform:none}}
dialog[open]{animation:pop .22s cubic-bezier(.2,.8,.2,1)}
@keyframes pop{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
.bar.dl i{position:relative;overflow:hidden}
.bar.dl i::after{content:"";position:absolute;inset:0;width:40%;
  background:linear-gradient(90deg,transparent,rgba(239,246,255,.45),transparent);animation:shimmer 1.2s linear infinite}
@keyframes shimmer{from{transform:translateX(-100%)}to{transform:translateX(250%)}}
.done-check{display:flex;align-items:center;gap:8px;color:var(--good);font-size:13px;font-weight:600}
.done-check svg{width:22px;height:22px}
.done-check path{stroke-dasharray:24;stroke-dashoffset:24;animation:draw .5s .1s ease-out forwards}
.done-check circle{transform-origin:center;animation:enter .3s both}
@keyframes draw{to{stroke-dashoffset:0}}

/* ---- files found in the install folder */
.found{padding:16px 18px;margin:0 0 16px;display:flex;flex-direction:column;gap:10px}
.found h3{margin:0;font-size:15.5px;font-weight:650}
.found .rows{display:flex;flex-direction:column;gap:2px;max-height:312px;overflow:auto}
.frow{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:10px;border:1px solid transparent;color:var(--text);font-size:14px}
label.frow{flex-direction:row;cursor:pointer} label.frow:hover{background:rgba(59,130,246,.10)}
.frow input{flex:none} .frow>span{min-width:0;flex:1}
.frow small{display:block;color:var(--faint);font-size:12px}
.frow button{min-height:34px;padding:5px 11px;font-size:13px}
.found .btns{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}

/* ---- catalog in the Add dialog */
.cat{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px;max-height:228px;overflow:auto}
.cat button{text-align:left;min-height:52px;padding:7px 11px;display:flex;flex-direction:column;align-items:flex-start;gap:1px}
.cat button small{color:var(--faint);font-size:11.5px;font-weight:400}
details.adv{border-radius:14px;border:1px solid var(--edge);background:rgba(5,8,20,.3)}
details.adv>summary{cursor:pointer;padding:10px 14px;font-size:13.5px;color:var(--dim);list-style-position:inside}
details.adv[open]>summary{border-bottom:1px solid var(--edge)}
details.adv>div{padding:12px 14px;display:flex;flex-direction:column;gap:12px}
.using{padding:10px 12px;border-radius:12px;border:1px solid rgba(96,165,250,.45);background:rgba(37,99,235,.14);font-size:13px}

/* ---- release notes */
.notes{white-space:pre-wrap;font-size:13.5px;line-height:1.5;color:var(--blue-100);padding:12px 14px;border-radius:14px;
  background:rgba(5,8,20,.4);border:1px solid var(--edge);overflow-wrap:anywhere}
.notes b{color:#fff}

/* ---- the ⋯ menu on each card */
.menu{position:fixed;z-index:15;min-width:230px;padding:6px;border-radius:16px;border:1px solid var(--edge-hi);
  box-shadow:inset 0 1px 0 var(--shine),0 18px 44px -12px rgba(0,0,0,.8);display:flex;flex-direction:column}
.menu button{background:none;border:0;box-shadow:none;text-align:left;min-height:40px;padding:8px 12px;border-radius:10px;font-size:14px}
.menu button:hover:not(:disabled),.menu button:focus-visible{background:rgba(59,130,246,.16)}
.menu button small{display:block;color:var(--faint);font-size:11.5px}
.menu hr{border:0;border-top:1px solid var(--edge);margin:4px 6px}
.summary:empty{display:none}
.found-line{display:flex;align-items:center;gap:8px;padding:8px 10px 8px 16px;margin:0 0 16px;font-size:13.5px;color:var(--dim)}
.found-line span{flex:1}
.found-line button{min-height:34px;padding:5px 11px;font-size:13px}

@media (max-width:640px){header{padding:12px 14px} main{padding:14px} .brand .sub{display:none}}
/* Users who turn transparency off get solid panels and no blur at all. */
@media (prefers-reduced-transparency:reduce){
  .glass,.frost,dialog,#toast{background:#0b1638;-webkit-backdrop-filter:none;backdrop-filter:none}
  .glass::before{display:none}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}
  .done-check path{stroke-dashoffset:0}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <symbol id="logo" viewBox="0 0 256 256">
    <rect x="8" y="8" width="240" height="240" rx="56" fill="#172554"/>
    <rect x="8" y="8" width="240" height="240" rx="56" fill="none" stroke="#1e40af" stroke-width="6"/>
    <path d="M128 52a76 76 0 0 1 70 46M206 70l-6 34-33-10M128 204a76 76 0 0 1-70-46M50 186l6-34 33 10" fill="none" stroke="#3b82f6" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M128 92v58M104 128l24 24 24-24" fill="none" stroke="#eff6ff" stroke-width="16" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M98 168h60" stroke="#93c5fd" stroke-width="12" stroke-linecap="round"/>
  </symbol>
  <symbol id="gear" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></g></symbol>
  <symbol id="i-up" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 18V6M6.5 11.5 12 6l5.5 5.5"/></g></symbol>
  <symbol id="i-ok" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-get" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/></g></symbol>
  <symbol id="i-err" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.8 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.8a2 2 0 0 0-3.4 0z"/></g></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></symbol>
  <symbol id="i-sync" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 0 0-14.3-4.9M4 13a8 8 0 0 0 14.3 4.9"/><path d="M5 3v4h4M19 21v-4h-4"/></g></symbol>
  <symbol id="i-more" viewBox="0 0 24 24"><g fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></g></symbol>
  <symbol id="i-busy" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 1-9 9" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></symbol>
</defs></svg>

<header class="frost">
  <div class="brand"><svg><use href="#logo"/></svg>
    <div><h1>DeckPort<span class="pill">v__VERSION__</span></h1><div class="sub" id="sub">AppImages &amp; PC ports, always up to date</div></div></div>
  <button id="btnCheck" class="icon ghost" title="Check for updates" aria-label="Check for updates"><svg><use href="#i-sync"/></svg></button>
  <button id="btnAdd"><svg><use href="#i-plus"/></svg> Add app</button>
  <button id="btnUpdAll" class="primary" hidden>Update all</button>
  <button id="btnSet" class="icon ghost" title="Settings" aria-label="Settings"><svg><use href="#gear"/></svg></button>
</header>
<main><div id="found"></div><div class="summary" id="summary"></div><div id="list" class="grid"></div></main>

<dialog id="dAdd"><form class="dlg" method="dialog" id="fAdd">
  <h2 id="addTitle">Add app</h2>
  <label id="aRepoLbl">Search popular apps, or paste a release link (GitHub, GitLab, Codeberg, Gitea, Forgejo)
    <input type="text" id="aRepo" placeholder="e.g. pcsx2, or https://github.com/owner/repo" autocomplete="off" spellcheck="false" required></label>
  <div class="cat" id="aCat"></div>
  <div class="hint" id="aInfo"></div>
  <div class="using" id="aUsing" hidden></div>
  <details class="adv" id="aAdv"><summary>Advanced: name, asset, pre-releases</summary><div>
    <label>Name<input type="text" id="aName" placeholder="Shown in the list, also the file name"></label>
    <label>Asset pattern (regex)<input type="text" id="aPat" placeholder="auto: picks the Linux AppImage / Linux build"></label>
    <label class="row"><input type="checkbox" id="aPre"> Include pre-releases / nightlies</label>
    <div class="assets" id="aAssets" hidden></div>
    <div class="hint" id="aTip" hidden>Click a file to download that one instead. DeckPort turns it into a pattern that keeps matching future versions.</div>
  </div></details>
  <div class="btns"><button type="button" class="ghost" onclick="dAdd.close()">Cancel</button><button class="primary" id="aSave" type="submit">Add</button></div>
</form></dialog>

<dialog id="dNotes"><form class="dlg" method="dialog">
  <h2 id="nTitle"></h2>
  <div class="hint" id="nMeta"></div>
  <div class="notes" id="nBody"></div>
  <div class="btns"><button type="button" class="ghost" onclick="dNotes.close()">Close</button><button type="button" class="primary" id="nUpd">Update now</button></div>
</form></dialog>

<dialog id="dSet"><form class="dlg" method="dialog" id="fSet">
  <h2>Settings</h2>
  <label>Install folder<input type="text" id="sDir"></label>
  <div class="hint">AppImages are saved with a fixed name (e.g. <span class="mono">Name.AppImage</span>) so your Steam shortcuts keep working after updates.</div>
  <label class="row"><input type="checkbox" id="sBak"> Keep previous version as <span class="mono">.old</span> (enables rollback)</label>
  <label>GitHub token (optional, raises rate limit from 60 to 5000 requests/hour)<input type="password" id="sTok" placeholder="ghp_… (leave empty to keep current)"></label>
  <div class="hint" id="sTokInfo"></div>
  <div class="sep"></div>
  <label>App list</label>
  <div class="btns" style="justify-content:flex-start;margin-top:-6px">
    <button type="button" id="sExport">Export list</button>
    <button type="button" id="sImport">Import list</button>
    <input type="file" id="sImportFile" accept=".json,application/json" hidden>
  </div>
  <div class="hint">The export contains only repos, names and asset patterns. No token, no files, no paths. Use it as a backup or to share your list.</div>
  <div class="sep"></div>
  <label>Storage</label>
  <div class="hint" id="sDisk" style="margin-top:-6px"></div>
  <div class="btns" style="justify-content:flex-start;margin-top:-4px">
    <button type="button" id="sClean">Delete .old backups</button>
    <button type="button" id="sUnhide">Show hidden files again</button>
  </div>
  <div class="btns" style="justify-content:space-between">
    <span><button type="button" id="sOpen">Open folder</button> <button type="button" class="danger ghost" id="sQuit">Quit DeckPort</button></span>
    <span><button type="button" class="ghost" onclick="dSet.close()">Cancel</button> <button class="primary" type="submit">Save</button></span>
  </div>
</form></dialog>

<dialog id="dAsk"><form class="dlg" method="dialog">
  <h2 id="askTitle"></h2><p id="askText"></p>
  <label class="row" id="askCheckRow"><input type="checkbox" id="askCheck"> <span id="askCheckText"></span></label>
  <div class="btns"><button type="button" class="ghost" id="askNo">Cancel</button><button type="button" class="primary" id="askYes">OK</button></div>
</form></dialog>
<div id="menu" class="menu frost" role="menu" hidden></div>
<div id="toast"></div>

<script>
const TOKEN="__TOKEN__";
// Opened by the desktop app (Tauri webview) rather than a browser tab.
const APP=new URLSearchParams(location.search).has("app");
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

// In-page confirm: native confirm() isn't reliable inside the desktop app's webview.
function ask({title,text="",ok="OK",danger=false,check=""}){
  return new Promise(resolve=>{
    const d=$("#dAsk"),yes=$("#askYes");let done=false;
    $("#askTitle").textContent=title;$("#askText").textContent=text;
    $("#askCheckRow").hidden=!check;$("#askCheck").checked=false;$("#askCheckText").textContent=check;
    yes.textContent=ok;yes.className=danger?"danger":"primary";
    const finish=v=>{if(done)return;done=true;d.close();resolve(v)};
    yes.onclick=()=>finish({checked:$("#askCheck").checked});
    $("#askNo").onclick=()=>finish(null);
    d.onclose=()=>finish(null);
    d.showModal();yes.focus();
  });
}

const ICON=(id,cls="")=>`<svg class="${cls}"><use href="#${id}"/></svg>`;
const seen=new Set(), doneAt={}; let prevJobs={}, FILTER="all";
try{FILTER=localStorage.getItem("deckport.filter")||"all"}catch(e){}

// One status per card; it drives the icon, colour, badge text, sort order and filter.
function status(a){
  const job=S.jobs[a.id], inst=a.installed&&a.exists;
  if(job&&job.state=="running") return "busy";
  if(a.error) return "err";
  if(!inst) return "new";
  if(a.has_update) return "update";
  return a.latest&&a.latest.tag?"ok":"busy";
}
const ORDER={update:0,busy:1,err:2,new:3,ok:4};
const BADGE={update:["i-up","st-update"],ok:["i-ok","st-ok"],new:["i-get","st-new"],err:["i-err","st-err"],busy:["i-busy",""]};

// Nightly builds are tagged with long hashes ("build-4024ea31e049f3…"): keep cards to one line.
const short=t=>{t=String(t||"");return t.length>16?t.slice(0,14)+"…":t};
function card(a,i){
  const job=S.jobs[a.id], inst=a.installed, here=!!(inst&&a.exists), L=a.latest||{}, st=status(a), [icon]=BADGE[st];
  const running=job&&job.state=="running";
  let label={update:`Update → ${esc(short(L.tag))}`,ok:a.skipped?`Skipped ${esc(short(L.tag))}`:"Up to date",new:"Not installed",err:"Error",busy:"Checking…"}[st];
  if(running) label=job.label=="Downloading"&&job.progress?`Downloading ${Math.round(job.progress*100)}%`:esc(job.label)+"…";
  const badge=`<span class="badge ${st=="update"&&!seen.has(a.id)?"pulse":""}">${ICON(icon,st=="busy"?"spin":"")}${label}</span>`;
  let jobHtml="";
  if(job){
    if(running){
      const dl=job.label=="Downloading"&&job.progress;
      jobHtml=`<div class="bar ${dl?"dl":"wait"}"><i style="transform:scaleX(${dl?job.progress.toFixed(3):1})"></i></div><div class="jobtxt">${esc(job.label)}… ${esc(job.message)}</div>`;
    }
    else if(job.state=="error") jobHtml=`<div class="err">${esc(job.message)}</div>`;
  }
  if(doneAt[a.id]&&Date.now()-doneAt[a.id]<4000&&!running)
    jobHtml=`<div class="done-check"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="rgba(134,239,172,.12)" stroke="currentColor" stroke-width="1.5"/><path d="M7 12.5l3.2 3.2L17 9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>Installed ${esc(inst&&inst.tag||"")}</div>`;
  const mainBtn=!here?"Install":(a.has_update?"Update":null);
  const isNew=!seen.has(a.id);
  // One line: the version you have and, if there is one, the version you'd get.
  const cur=here?(inst.tag=="unknown"?"your copy":inst.tag):"";
  let ver="";
  if(st=="update")ver=`<span class="mono" title="${esc(L.tag)}">${esc(short(cur))} → <b>${esc(short(L.tag))}</b></span>`;
  else if(here)ver=`<span class="mono" title="${esc(inst.tag)}">${esc(short(cur))}</span>`+(a.skipped?`<span class="dim">${esc(short(L.tag))} skipped</span><a href="#" data-a="unskip" data-id="${a.id}">undo</a>`:"");
  else if(L.tag)ver=`<span class="mono" title="${esc(L.tag)}">${esc(short(L.tag))}</span>`+(L.asset&&L.asset.size?`<span class="dim">${mb(L.asset.size)}</span>`:"");
  if(ver&&L.prerelease)ver+='<span class="tag">pre-release</span>';
  if(st=="update"&&L.notes)ver+=`<a href="#" data-a="notes" data-id="${a.id}">What's new</a>`;
  if(here&&inst.verified)ver+=`<span class="tag ok" title="SHA-256 ${esc(inst.sha256)} (${esc(inst.verified_by)})">✓ verified</span>`;
  if(a.in_steam)ver+='<span class="tag steam">in Steam</span>';
  // A file the user brought keeps its own (often versioned) name, so show which one it is.
  const fname=a.target_path&&a.file_name?`<div class="fname mono" title="Your file, updated in place">${esc(a.file_name)}</div>`:"";
  return `<div class="card glass st-${st} ${isNew?"enter":""}" style="${isNew?`animation-delay:${Math.min(i,8)*45}ms`:""}">
   <div class="top"><div class="avatar">${esc((a.name||"?").trim().charAt(0).toUpperCase())}</div>
     <div style="min-width:0"><div class="name">${esc(a.name)}</div>
     <a class="repo" href="${esc(a.web_url)}" target="_blank" rel="noopener">${esc(a.display_repo)}</a></div>${badge}</div>
   ${ver?`<div class="ver">${ver}</div>`:""}
   ${fname}
   ${a.error&&!job?`<div class="err">${esc(a.error)}</div>`:""}
   ${jobHtml}
   <div class="actions">
     ${mainBtn?`<button class="primary" data-a="update" data-id="${a.id}" ${running||!L.asset?"disabled":""}>${ICON(mainBtn=="Update"?"i-up":"i-get")} ${mainBtn}</button>`:""}
     ${here?`<button data-a="launch" data-id="${a.id}" ${running?"disabled":""}>${inst.kind=="archive"?"Open":"Launch"}</button>`:""}
     <button class="ghost icon more" data-a="menu" data-id="${a.id}" title="More" aria-label="More actions for ${esc(a.name)}" aria-haspopup="menu">${ICON("i-more")}</button>
   </div></div>`;
}

function render(){
  renderFound();
  // Remember installs that just finished, for the check-mark animation.
  for(const [id,p] of Object.entries(prevJobs)){
    const j=S.jobs[id];
    if(p.state=="running"&&p.label!="Checking"&&j&&j.state=="done") doneAt[id]=Date.now();
  }
  prevJobs=S.jobs||{};
  const L=$("#list"), apps=[...S.apps].sort((x,y)=>ORDER[status(x)]-ORDER[status(y)]||x.name.localeCompare(y.name));
  const count=k=>apps.filter(a=>status(a)==k).length;
  const chips=[["all","All",apps.length,"var(--blue-300)"],["update","Updates",count("update"),"var(--blue-400)"],
               ["ok","Up to date",count("ok"),"var(--good)"],["new","Not installed",count("new"),"var(--warn)"]];
  if(count("err")) chips.push(["err","Errors",count("err"),"var(--bad)"]);
  if(!chips.some(c=>c[0]==FILTER)) FILTER="all";
  // Filters only help with a long list; a short one is already sorted with updates first.
  const useChips=apps.length>=8;
  if(!useChips)FILTER="all";
  $("#summary").innerHTML=useChips?chips.map(([k,t,n,c])=>`<button class="${FILTER==k?"on":""}" data-f="${k}" style="--c:${c}"><i></i>${t} <b>${n}</b></button>`).join(""):"";
  const shown=FILTER=="all"?apps:apps.filter(a=>status(a)==FILTER);
  if(!S.apps.length&&!foundLater&&(S.found||[]).some(f=>f.src)){L.innerHTML=""}  // the found banner is the next step
  else if(!S.apps.length){L.innerHTML='<div class="empty glass enter" style="grid-column:1/-1"><svg><use href="#logo"/></svg><b>No apps yet</b>Click “Add app” to pick a popular emulator or port, or paste a link to any release page.</div>'}
  else if(!shown.length){L.innerHTML='<div class="empty glass" style="grid-column:1/-1"><b>Nothing here</b>No apps match this filter.</div>'}
  else L.innerHTML=shown.map(card).join("");
  shown.forEach(a=>seen.add(a.id));
  // Safety net: if the webview pauses animations (hidden/occluded window), still show the cards.
  setTimeout(()=>L.querySelectorAll(".enter").forEach(el=>el.classList.remove("enter")),1500);
  const n=count("update");
  const b=$("#btnUpdAll");b.hidden=!n;b.textContent=`Update all (${n})`;
  const checking=Object.values(S.jobs||{}).some(j=>j.state=="running"&&j.label=="Checking");
  const last=Math.max(0,...S.apps.map(a=>a.checked_at||0));
  $("#btnCheck svg").classList.toggle("spin",checking);
  $("#sub").textContent=checking?"Checking for updates…":S.apps.length?`Checked ${ago(last)}`:"AppImages & PC ports, always up to date";
}
// Drop the entrance class once it has played so a card can never stay mid-animation.
$("#list").addEventListener("animationend",e=>{if(e.target.classList.contains("enter")&&e.animationName=="enter")e.target.classList.remove("enter")});
$("#summary").addEventListener("click",e=>{
  const c=e.target.closest("button[data-f]");if(!c)return;
  FILTER=c.dataset.f;try{localStorage.setItem("deckport.filter",FILTER)}catch(err){}
  render();
});
let autoChecked=false;
async function refresh(){
  try{
    S=await api("/api/state");render();
    if(!autoChecked){
      // Opening DeckPort checks for updates by itself, at most once an hour.
      autoChecked=true;const last=Math.max(0,...S.apps.map(a=>a.checked_at||0));
      if(S.apps.length&&Date.now()/1000-last>3600)act("/api/check",{});
    }
  }catch(e){$("#list").innerHTML='<div class="empty glass" style="grid-column:1/-1"><b>DeckPort is not running</b>Start it again from the app menu.</div>'}
  clearTimeout(timer);
  const busy=Object.values(S.jobs||{}).some(j=>j.state=="running");
  timer=setTimeout(refresh,busy?700:(Object.keys(S.jobs||{}).length?2000:15000));
}

async function doAction(k,a,el){
  const id=a.id;
  if(k=="update"||k=="check"){if(el)el.disabled=true}
  if(k=="update")act("/api/update",{id});
  else if(k=="check")act("/api/check",{id});
  else if(k=="launch")act("/api/launch",{id},"Launching "+a.name);
  else if(k=="menu")openMenu(el,a);
  else if(k=="rollback"){if(await ask({title:`Roll back ${a.name}?`,text:"The previous version (.old) will be restored.",ok:"Roll back"}))act("/api/rollback",{id},"Rolled back")}
  else if(k=="remove"){
    const r=await ask({title:`Remove ${a.name}?`,text:"DeckPort will stop tracking it.",ok:"Remove",danger:true,
                       check:a.installed&&a.exists?"Also delete its downloaded files":""});
    if(r)act("/api/remove",{id,delete_files:!!r.checked},"Removed");
  }
  else if(k=="edit")openAdd(a);
  else if(k=="notes")openNotes(a);
  else if(k=="release"){const u=(a.latest&&a.latest.url)||a.web_url;APP?act("/api/open_url",{url:u}):window.open(u,"_blank","noopener")}
  else if(k=="skip")act("/api/skip",{id},`Skipped ${a.latest.tag}. DeckPort will tell you about the next version.`);
  else if(k=="unskip")act("/api/skip",{id,undo:true},`${a.name}: update offered again`);
  else if(k=="steam")act("/api/steam_add",{id},r=>r.added?`${a.name} added to Steam. Start Steam to see it.`:`${a.name} is already in Steam`);
}
$("#list").addEventListener("click",e=>{
  const b=e.target.closest("[data-a]");if(!b)return;
  if(b.tagName=="A")e.preventDefault();
  const a=S.apps.find(x=>x.id==b.dataset.id);if(a)doAction(b.dataset.a,a,b);
});

// The ⋯ menu: everything a card needs now and then, so the card itself shows only what matters.
const menu=$("#menu");let menuFor=null;
function closeMenu(){if(!menu.hidden){menu.hidden=true;if(menuFor)menuFor.focus();menuFor=null}}
function openMenu(btn,a){
  if(menuFor==btn){closeMenu();return}
  const L=a.latest||{},st=status(a),here=a.installed&&a.exists,running=st=="busy";
  const it=(k,t,{hint="",danger=false,off=false}={})=>`<button type="button" role="menuitem" data-k="${k}" class="${danger?"danger":""}" ${off?"disabled":""}>${t}${hint?`<small>${hint}</small>`:""}</button>`;
  const rows=[it("check","Check for updates",{off:running})];
  if(L.notes&&st!="update")rows.push(it("notes",`Release notes ${esc(L.tag||"")}`));
  if(st=="update")rows.push(it("skip",`Skip ${esc(L.tag||"")}`,{hint:"Get told about the next version instead"}));
  if(a.skipped)rows.push(it("unskip",`Offer ${esc(L.tag||"")} again`));
  if(a.has_backup)rows.push(it("rollback","Roll back to previous version",{off:running}));
  if(S.steam&&here&&a.installed.kind!="archive"&&!a.in_steam)
    rows.push(it("steam","Add to Steam",{off:S.steam_running,hint:S.steam_running?"Close Steam first (Steam menu > Exit)":"Adds it as a Non-Steam game"}));
  rows.push(it("release","Open release page"),"<hr>",it("edit","Edit"),it("remove","Remove…",{danger:true}));
  menu.innerHTML=rows.join("");menu.hidden=false;menuFor=btn;menu.dataset.id=a.id;
  const r=btn.getBoundingClientRect(),m=menu.getBoundingClientRect();
  menu.style.left=Math.max(8,Math.min(r.right-m.width,innerWidth-m.width-8))+"px";
  menu.style.top=(r.bottom+6+m.height>innerHeight?Math.max(8,r.top-6-m.height):r.bottom+6)+"px";
  const first=menu.querySelector("button:not(:disabled)");if(first)first.focus();
}
menu.addEventListener("click",e=>{
  const b=e.target.closest("button[data-k]");if(!b||b.disabled)return;
  const a=S.apps.find(x=>x.id==menu.dataset.id);menuFor=null;menu.hidden=true;
  if(a)doAction(b.dataset.k,a);
});
document.addEventListener("click",e=>{if(!menu.hidden&&!menu.contains(e.target)&&!e.target.closest("[data-a=menu]"))closeMenu()});
document.addEventListener("keydown",e=>{
  if(menu.hidden)return;
  if(e.key=="Escape")closeMenu();
  else if(e.key=="ArrowDown"||e.key=="ArrowUp"){
    e.preventDefault();const bs=[...menu.querySelectorAll("button:not(:disabled)")],i=bs.indexOf(document.activeElement);
    bs[(i+(e.key=="ArrowDown"?1:bs.length-1))%bs.length].focus();
  }
});
addEventListener("resize",closeMenu);addEventListener("scroll",closeMenu,true);

// Release notes, lightly cleaned up from Markdown into plain text.
function md(t){
  return esc(String(t).replace(/\r/g,"").replace(/<[^>]+>/g,"")).split("\n").map(l=>{
    l=l.replace(/!\[[^\]]*\]\([^)]*\)/g,"").replace(/\[([^\]]+)\]\([^)]*\)/g,"$1").replace(/\*\*(.+?)\*\*/g,"$1").replace(/`/g,"");
    const h=l.match(/^\s{0,3}#{1,6}\s+(.*)/);if(h)return"<b>"+h[1]+"</b>";
    return l.replace(/^(\s*)[-*+]\s+/,"$1• ");
  }).join("\n").replace(/\n{3,}/g,"\n\n").trim();
}
function openNotes(a){
  const L=a.latest||{},st=status(a);
  $("#nTitle").textContent=`${a.name} ${L.tag||""}`;
  $("#nMeta").innerHTML=(L.published?"Released "+esc(L.published.slice(0,10))+" · ":"")+`<a href="${esc(L.url||a.web_url)}" target="_blank" rel="noopener">Open release page</a>`;
  $("#nBody").innerHTML=md(L.notes||"")||'<span class="dim">No release notes.</span>';
  const u=$("#nUpd");u.hidden=!(st=="update"||st=="new"||a.skipped)||!L.asset;
  u.textContent=a.installed&&a.exists?"Update now":"Install";
  u.onclick=()=>{dNotes.close();act("/api/update",{id:a.id})};
  dNotes.showModal();$("#nBody").scrollTop=0;
}

// Banner: AppImages already in the install folder that DeckPort could keep up to date.
let foundKey="",foundLater=false,foundOpen=false;
function renderFound(){
  const F=S.found||[],box=$("#found");
  if(foundLater||!F.length){box.innerHTML="";foundKey="";return}
  const key=JSON.stringify([foundOpen,F.map(f=>[f.path,f.app_id||"",f.src])]);
  if(key==foundKey)return;  // keep the user's checkbox choices between polls
  foundKey=key;
  const known=F.filter(f=>f.src),unknown=F.filter(f=>!f.src);
  if(!known.length&&!foundOpen){
    // Only files DeckPort can't name: one quiet line instead of a big banner.
    box.innerHTML=`<div class="found-line glass"><span>${F.length} AppImage${F.length>1?"s":""} in ${esc(S.settings.install_dir_short||"")} DeckPort doesn't recognize</span>
      <button type="button" class="ghost" id="fOpen">Review</button><button type="button" class="ghost" id="fHideAll">Hide</button></div>`;
    return;
  }
  const row=f=>f.src
    ?`<label class="frow"><input type="checkbox" data-p="${esc(f.path)}" checked><span><b>${esc(f.name)}</b> <span class="dim">${f.app_id?"for the "+esc(f.name)+" entry you added":esc(f.display)}</span><small class="mono">${esc(f.file)} · ${mb(f.size)}</small></span></label>`
    :`<div class="frow"><span><span class="mono">${esc(f.file)}</span><small>Not recognized · ${mb(f.size)}</small></span><button type="button" data-link="${esc(f.path)}">Choose repo…</button><button type="button" class="ghost" data-ign="${esc(f.path)}">Hide</button></div>`;
  box.innerHTML=`<div class="found glass"><h3>Found ${F.length} AppImage${F.length>1?"s":""} in ${esc(S.settings.install_dir_short||"")}</h3>
    <div class="hint">DeckPort can keep these up to date. Updates keep the same file name, so Steam shortcuts to them keep working.</div>
    <div class="rows">${known.map(row).join("")}${unknown.map(row).join("")}</div>
    <div class="btns"><button type="button" class="ghost" id="fLater">Not now</button>${known.length?`<button type="button" class="primary" id="fTrack">Keep ${known.length} up to date</button>`:""}</div></div>`;
}
$("#found").addEventListener("change",()=>{
  const n=$("#found").querySelectorAll("input[data-p]:checked").length,b=$("#fTrack");
  if(b){b.disabled=!n;b.textContent=`Keep ${n} up to date`}
});
$("#found").addEventListener("click",async e=>{
  const t=e.target.closest("button");if(!t)return;
  const byPath=p=>(S.found||[]).find(f=>f.path==p);
  if(t.id=="fLater"){foundLater=true;renderFound();render()}
  else if(t.id=="fOpen"){foundOpen=true;renderFound()}
  else if(t.id=="fHideAll"){foundKey="";act("/api/scan_ignore",{paths:(S.found||[]).filter(f=>!f.src).map(f=>f.path)},"Hidden. Settings can show them again.")}
  else if(t.id=="fTrack"){
    const items=[...$("#found").querySelectorAll("input[data-p]:checked")].map(i=>byPath(i.dataset.p)).filter(Boolean)
      .map(f=>({path:f.path,src:f.src,name:f.name,app_id:f.app_id||""}));
    t.disabled=true;foundKey="";
    act("/api/track_found",{items},r=>`Tracking ${r.count} app(s), checking for updates…`);
  }
  else if(t.dataset.link){const f=byPath(t.dataset.link);if(f)openAdd(null,{path:f.path,file:f.file})}
  else if(t.dataset.ign){foundKey="";act("/api/scan_ignore",{paths:[t.dataset.ign]},"Hidden. Settings can show it again.")}
});
$("#btnCheck").onclick=()=>act("/api/check",{});
$("#btnUpdAll").onclick=()=>act("/api/update",{},r=>`Updating ${r.count} app(s)`);
if(APP){
  // Links open in the system browser instead of navigating the app window.
  document.addEventListener("click",e=>{
    const a=e.target.closest("a[target=_blank]");if(!a)return;
    e.preventDefault();act("/api/open_url",{url:a.href});
  });
}

let ADOPT=null,prevTimer=null,lastPrev="",PICKED=false;
const isLink=v=>/[\/.]/.test(v.trim());  // owner/repo or a URL; anything else is a catalog search
function openAdd(a,adopt){
  editId=a?a.id:null;ADOPT=adopt||null;
  $("#addTitle").textContent=a?"Edit "+a.name:"Add app";
  $("#aSave").textContent=a?"Save":"Add";
  $("#aRepoLbl").firstChild.textContent=a?"Repository":"Search popular apps, or paste a release link (GitHub, GitLab, Codeberg, Gitea, Forgejo)";
  // Non-GitHub repos need their host, or the preview would look them up on GitHub.
  $("#aRepo").value=a?(a.host&&a.host!="github.com"?`https://${a.host}/${a.repo}`:a.repo):"";$("#aRepo").disabled=!!a;
  PICKED=false;
  $("#aName").value=a?a.name:"";$("#aPat").value=a?a.pattern||"":"";$("#aPre").checked=a?!!a.prerelease:false;
  $("#aAssets").hidden=true;$("#aTip").hidden=true;$("#aInfo").textContent="";lastPrev="";
  $("#aAdv").open=!!a;
  $("#aUsing").hidden=!ADOPT;
  if(ADOPT)$("#aUsing").innerHTML=`DeckPort will keep <span class="mono">${esc(ADOPT.file)}</span> up to date. It keeps its name, so a Steam shortcut to it keeps working.`;
  renderCat();dAdd.showModal();
  if(a)preview();else $("#aRepo").focus();
}
$("#btnAdd").onclick=()=>openAdd(null);
function renderCat(){
  const q=$("#aRepo").value.trim().toLowerCase(),box=$("#aCat");
  if(editId||isLink(q)){box.hidden=true;return}
  const have=new Set(S.apps.map(a=>(a.host+"/"+a.repo).toLowerCase()));
  const list=(S.catalog||[]).map((c,i)=>({...c,i})).filter(c=>!q||(c.name+" "+c.about).toLowerCase().includes(q));
  box.hidden=!list.length;
  box.innerHTML=list.map(c=>{const t=have.has(c.key);
    return `<button type="button" data-c="${c.i}" ${t?"disabled":""}>${esc(c.name)}<small>${esc(c.about)}${t?" · already added":""}</small></button>`}).join("");
}
$("#aCat").onclick=e=>{
  const b=e.target.closest("[data-c]");if(!b)return;const c=S.catalog[b.dataset.c];
  $("#aRepo").value=c.src;$("#aName").value=c.name;PICKED=true;renderCat();preview();
};
const reEsc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
function patternFor(name){
  // Replace version-like chunks with wildcards so the pattern keeps matching future releases.
  return "^"+reEsc(name).replace(/v?\d+(\\\.\d+)+[\w-]*?(?=[-_\\]|$)/g,".*").replace(/(\.\*)+/g,".*")+"$";
}
function previewSoon(){clearTimeout(prevTimer);prevTimer=setTimeout(preview,600)}
$("#aRepo").oninput=()=>{
  // Typing after picking from the catalog starts over: don't keep that app's name.
  if(PICKED){PICKED=false;$("#aName").value=""}
  renderCat();
  if(isLink($("#aRepo").value))previewSoon();
  else{clearTimeout(prevTimer);lastPrev="";$("#aInfo").textContent="";$("#aAssets").hidden=true;$("#aTip").hidden=true}
};
$("#aPat").oninput=previewSoon;$("#aPre").onchange=preview;
async function preview(){
  const repo=$("#aRepo").value.trim();if(!isLink(repo))return;
  const me=lastPrev=JSON.stringify([repo,$("#aPat").value,$("#aPre").checked]),box=$("#aAssets");
  $("#aInfo").textContent="Looking up the latest release…";
  try{
    const r=await api("/api/preview",{repo,pattern:$("#aPat").value,prerelease:$("#aPre").checked});
    if(me!=lastPrev)return;  // a newer lookup is on its way
    $("#aInfo").innerHTML=`<b>${esc(r.repo)}</b> · ${esc(r.tag)} · `+(r.picked?`will download <span class="mono">${esc(r.picked)}</span>`:"⚠ no Linux build found. Open Advanced and click the right file.");
    box.innerHTML=r.assets.map(x=>`<div class="${x.name==r.picked?"pick":""}" data-n="${esc(x.name)}">${esc(x.name)}<small>${mb(x.size)}</small></div>`).join("")||"<div>No files in this release</div>";
    box.hidden=false;$("#aTip").hidden=false;
    if(!r.picked)$("#aAdv").open=true;
  }catch(e){if(me!=lastPrev)return;$("#aInfo").textContent="⚠ "+e.message;box.hidden=true;$("#aTip").hidden=true}
}
$("#aAssets").onclick=e=>{const d=e.target.closest("[data-n]");if(!d)return;$("#aPat").value=patternFor(d.dataset.n);preview()};
$("#fAdd").onsubmit=async e=>{
  e.preventDefault();
  if(!editId&&!isLink($("#aRepo").value)){
    // Enter in the search box picks the first match instead of adding "pcsx2" as a repo.
    const b=document.querySelector("#aCat button:not(:disabled)");
    if(b)b.click();else toast("Pick an app from the list, or paste a link",true);
    return;
  }
  const body={name:$("#aName").value,pattern:$("#aPat").value,prerelease:$("#aPre").checked,adopt_path:ADOPT?ADOPT.path:""};
  try{
    if(editId)await api("/api/edit",{id:editId,...body});
    else await api("/api/add",{repo:$("#aRepo").value,...body});
    dAdd.close();foundKey="";toast(ADOPT?"Added, using your existing file":editId?"Saved":"Added, checking…");refresh();
  }catch(err){toast(err.message,true)}
};

$("#btnSet").onclick=()=>{
  $("#sDir").value=S.settings.install_dir||"";$("#sBak").checked=!!S.settings.keep_backup;$("#sTok").value="";
  $("#sTokInfo").innerHTML=S.settings.has_token?'Token saved. <a href="#" id="clrTok">Remove token</a>':"No token set.";
  const c=$("#clrTok");if(c)c.onclick=ev=>{ev.preventDefault();act("/api/settings",{clear_token:true},"Token removed");dSet.close()};
  const sum=k=>S.apps.reduce((t,a)=>t+(a[k]||0),0),old=sum("old_disk"),n=S.settings.scan_ignored||0;
  $("#sDisk").textContent=`Apps use ${size(sum("disk"))}. Previous versions (.old, for Rollback) use ${size(old)}.`;
  $("#sClean").disabled=!old;
  $("#sUnhide").hidden=!n;$("#sUnhide").textContent=`Show ${n} hidden file${n>1?"s":""} again`;
  dSet.showModal();
};
const size=b=>b>=1e9?(b/1e9).toFixed(1)+" GB":(b/1e6).toFixed(0)+" MB";
$("#sClean").onclick=async()=>{
  if(!await ask({title:"Delete .old backups?",text:"This frees disk space, but Rollback won't be possible until the next update.",ok:"Delete",danger:true}))return;
  act("/api/clean_backups",{},r=>`Deleted ${r.removed} backup(s), freed ${size(r.freed)}`);dSet.close();
};
$("#sUnhide").onclick=()=>{foundKey="";foundLater=false;act("/api/scan_ignore",{clear:true},"Hidden files are shown again");dSet.close()};
$("#fSet").onsubmit=e=>{e.preventDefault();act("/api/settings",{install_dir:$("#sDir").value,keep_backup:$("#sBak").checked,github_token:$("#sTok").value},"Settings saved");dSet.close()};
$("#sOpen").onclick=()=>act("/api/open",{});
$("#sExport").onclick=async()=>{
  try{
    if(APP){const r=await api("/api/export_save");toast(`Exported ${r.count} app(s) to ${r.path}`);return}
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
// In the desktop app, closing the window quits; the button is only for the browser version.
$("#sQuit").hidden=APP;
$("#sQuit").onclick=async()=>{try{await api("/api/quit")}catch(e){}document.body.innerHTML='<div class="empty"><b>DeckPort closed</b>You can close this tab.</div>'};
refresh();
</script></body></html>
"""


if __name__ == "__main__":
    main()
