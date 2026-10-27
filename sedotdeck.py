#!/usr/bin/env python3
"""
SedotDeck - Obtainium-style updater for AppImages & PC ports on Steam Deck.

Track GitHub Releases, check for updates, and download/replace them in one place.
Pure Python standard library: no pip installs needed on SteamOS.

Usage:
  sedotdeck.py serve                 # open the web UI (default)
  sedotdeck.py add <github-url>      # track a repo
  sedotdeck.py list
  sedotdeck.py scan [--track]        # find AppImages you already have
  sedotdeck.py check
  sedotdeck.py update [name ...]     # update given apps, or everything with an update
  sedotdeck.py remove <name>
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
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

VERSION = "0.0.2-alpha"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sedotdeck"
LEGACY_CONFIG_DIR = CONFIG_DIR.with_name("deckport")  # SedotDeck was called DeckPort before 0.0.1-alpha
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_PORT = 8765
USER_AGENT = "SedotDeck/" + VERSION

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")
# Assets that are clearly not for a Steam Deck (x86_64 Linux).
NOT_FOR_DECK = re.compile(
    r"(windows|win32|win64|[-_.]win[-_.]|\.exe$|\.msi$|macos|mac[-_.]?os|darwin|osx|\.dmg$|\.pkg$"
    r"|android|\.apk$|[-_.]ios[-_.]|aarch64|arm64|armhf|armv\d|[-_.]arm([-_.]?\d+|el)?([-_. )]|$)|i386|i686|riscv|ppc64|s390x|\.zsync$|\.sha\d*(sum)?$|\.sig$|\.asc$"
    r"|checksums?|\.blockmap$|\.txt$|\.json$|\.ya?ml$|\.deb$|\.rpm$|flatpak(?!$)|\.snap$|debug|[-_.]dbg([-_.]|$)|symbols|\.pdb$)",
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


def migrate_legacy_config():
    """Move ~/.config/deckport to ~/.config/sedotdeck once, so the app list survives the rename."""
    if not CONFIG_DIR.exists() and LEGACY_CONFIG_DIR.is_dir():
        try:
            os.replace(LEGACY_CONFIG_DIR, CONFIG_DIR)
        except OSError as e:
            print("Warning: could not move %s (%s)" % (LEGACY_CONFIG_DIR, e), file=sys.stderr)


def load_config():
    with _lock:
        migrate_legacy_config()
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


def fetch_release(src, prerelease, token="", skip_titles="", latest_only=False):
    """Latest release of src={provider, host, repo}, normalized to one shape for all forges.
    skip_titles: regex; releases whose title or tag match it are ignored (e.g. "nightly").
    latest_only: take only the release the developer marked Latest, never another one."""
    provider, host, repo = src["provider"], src["host"], src["repo"]
    skip = re.compile(skip_titles, re.I) if skip_titles else None
    wanted = lambda r: not (skip and (skip.search(r["name"]) or skip.search(r["tag_name"])))
    per_page = 30 if skip else 15  # filtered-out nightlies shouldn't use up the whole page
    tok, svc = (token, "github") if provider == "github" else ("", "")
    if provider == "github":
        api = "https://api.github.com/repos/%s/releases" % repo
        norm, list_url, latest_url = _norm_github, api + "?per_page=%d" % per_page, api + "/latest"
    elif provider == "gitea":
        api = "https://%s/api/v1/repos/%s/releases" % (host, repo)
        norm, list_url, latest_url = _norm_gitea, api + "?limit=%d" % per_page, api + "/latest"
    elif provider == "gitlab":
        api = "https://%s/api/v4/projects/%s/releases" % (host, urllib.parse.quote(repo, safe=""))
        norm = lambda r: _norm_gitlab(r, host, repo)
        list_url, latest_url = api + "?per_page=%d" % per_page, api + "/permalink/latest"
    else:
        raise RuntimeError("Unknown provider %r" % provider)
    try:
        # GitHub's Latest marker is the default for stable releases; elsewhere only when asked.
        if latest_only or (provider == "github" and not prerelease):
            try:
                r = norm(http_json(latest_url, tok, svc))
            except LookupError:
                if latest_only:
                    raise RuntimeError("%s has no release marked Latest" % repo)
                r = None  # repo may only have pre-releases; fall through to the list
            if r and wanted(r):
                return r
            if latest_only:
                raise RuntimeError("The release marked Latest (%s) matches your skip filter" % (r["name"] or r["tag_name"]))
        rels = [norm(r) for r in http_json(list_url, tok, svc)]
    except LookupError:
        raise RuntimeError("Repo %s not found on %s" % (repo, host))
    rels = [r for r in rels if not r["draft"] and wanted(r)]
    if not prerelease:
        rels = [r for r in rels if not r["prerelease"]] or rels
    if not rels:
        raise RuntimeError("No releases found in %s" % repo + (" after skipping %r" % skip_titles if skip else ""))
    return rels[0]


def moved_repo(app, rel):
    """The repo a release says it lives in now, when that isn't app["repo"] (the repo was renamed or moved)."""
    try:
        _prov, host, repo = parse_source(rel.get("html_url") or "")
    except ValueError:
        return ""
    if host != app.get("host", "github.com") or repo.lower() == app["repo"].lower():
        return ""
    return repo


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
    elif n.endswith(".flatpak"):
        s += 60  # installs cleanly, but an AppImage needs no runtime download
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
        rel = fetch_release(app, app.get("prerelease", False), token if app.get("provider") == "github" else "",
                            app.get("skip_titles", ""), app.get("latest_only", False))
        asset = pick_asset(rel.get("assets", []), app.get("pattern", ""))
        app["latest"] = release_info(rel, asset)
        moved = moved_repo(app, rel)
        if moved:
            app["moved_to"] = moved
        else:
            app.pop("moved_to", None)
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
    if n.endswith(".flatpak"):
        return "flatpak"
    if n.endswith(ARCHIVE_EXTS):
        return "archive"
    return "file"


def sniff_kind(path, name):
    """Kind from the file's magic bytes; GitLab asset names are often labels like "Linux (x86_64)"."""
    if name.lower().endswith(".flatpak"):
        return "flatpak"
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


LAUNCHER_NAME = "sedotdeck-launch.sh"  # written inside a folder port; never a launch candidate itself
_NOT_MAIN_DIRS = {"lib", "lib32", "lib64", "libs", "plugins", "platforms", "share", "python"}


# Windows helpers that are never the game itself.
_NOT_MAIN_EXE = re.compile(r"unins|crash|setup|install|redist|dxsetup|updater|reporter")


def launch_candidates(folder, wants=()):
    """Programs inside a folder port, best guess first: Linux binaries and scripts, then Windows .exe
    files (Steam runs those through Proton). "auto" marks the ones SedotDeck may pick by itself."""
    wants = {w for w in wants if len(w) >= 3}
    out = []
    for root, dirs, files in os.walk(folder):
        rel_root = "" if root == folder else os.path.relpath(root, folder)
        depth = rel_root.count(os.sep) + 1 if rel_root else 0
        dirs[:] = [] if depth >= 3 else sorted(dirs)
        in_lib = any(d.lower() in _NOT_MAIN_DIRS for d in rel_root.split(os.sep) if d)
        for fn in sorted(files):
            if not rel_root and fn == LAUNCHER_NAME:
                continue
            p, low = os.path.join(root, fn), fn.lower()
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode) or re.search(r"\.so(\.\d+)*$", low):
                    continue
                with open(p, "rb") as f:
                    head = f.read(4)
            except OSError:
                continue
            elf, ext = head == b"\x7fELF", os.path.splitext(low)[1]
            if ext == ".exe" and head[:2] == b"MZ":
                kind = "windows"
            elif (elf or head[:2] == b"#!") and (st.st_mode & 0o111 or not ext) or ext in (".sh", ".x86_64"):
                kind = "linux"
            else:
                continue
            st_ = stem_of(re.sub(r"\.(sh|exe)$", "", low))
            named = 2 if st_ in wants else int(len(st_) >= 3 and any(st_.startswith(w) or w.startswith(st_) for w in wants))
            junk = kind == "windows" and bool(_NOT_MAIN_EXE.search(low))
            key = (kind == "linux", not in_lib, not junk, named, int(elf or ext == ".x86_64"), -depth, st.st_size)
            out.append({"key": key, "path": p, "file": os.path.relpath(p, folder), "kind": kind,
                        "size": st.st_size, "auto": not in_lib and not junk})
    out.sort(key=lambda c: c["key"], reverse=True)
    for c in out:
        del c["key"]
    return out


def find_main_exe(folder, wants):
    """The program SedotDeck picks by itself in a folder port, preferring a Linux one named like the app."""
    best = next((c for c in launch_candidates(folder, wants) if c["auto"]), None)
    return best["path"] if best else None


def _app_wants(app):
    return stem_of(app["name"]), stem_of((app.get("repo") or "").split("/")[-1])


def write_launcher(app, install_dir, folder):
    """Point a folder port at its program, recorded in app["launch"]. A Linux one gets a stable
    <folder>/sedotdeck-launch.sh, pinned in app["launcher"] so a Steam shortcut to it survives updates
    (updates only prune files the old release shipped). A launcher from before, in <install_dir>, moves
    into the folder unless a Steam shortcut still points at it. A Windows .exe has no launcher: Steam
    starts it through Proton. A program the user picked (launch.manual) wins while it still exists;
    otherwise SedotDeck picks one."""
    cur = app.get("launch") or {}
    pick = None
    if cur.get("manual"):
        p = os.path.join(folder, cur.get("file") or "")
        pick = next((c for c in launch_candidates(folder) if c["path"] == p), None)
        if not pick:
            app["notice"] = "%s is gone in this version, so SedotDeck picked the program again" % cur.get("file")
    if not pick:
        pick = next((c for c in launch_candidates(folder, _app_wants(app)) if c["auto"]), None)
    if not pick:
        app.pop("launch", None)
        return None
    app["launch"] = {"file": pick["file"], "kind": pick["kind"], "manual": bool(cur.get("manual")) and pick["file"] == cur.get("file")}
    path = app.get("launcher")
    if pick["kind"] == "windows":
        if path and os.path.isfile(path):
            os.remove(path)
        return None
    exe = pick["path"]
    make_executable(exe)  # zips often drop the exec bit
    inside = os.path.join(folder, LAUNCHER_NAME)
    if path and path != inside and path not in steam_shortcut_exes():
        if os.path.isfile(path):
            os.remove(path)
        path = None
    path = path or inside
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("#!/bin/sh\ncd %s || exit 1\nexec ./%s \"$@\"\n" % (
            shlex.quote(os.path.dirname(exe)), shlex.quote(os.path.basename(exe))))
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    app["launcher"] = path
    return path


def set_launch_file(app, settings, file):
    """Use this program (a path relative to the app folder) for Launch and Steam; "" goes back to automatic."""
    folder = (app.get("installed") or {}).get("path") or ""
    if not os.path.isdir(folder) or is_flatpak(app):
        raise ValueError("%s is not installed as a folder" % app["name"])
    if file:
        if not any(c["file"] == file for c in launch_candidates(folder)):
            raise ValueError("%s is not a program SedotDeck can launch" % file)
        app["launch"] = {"file": file, "manual": True}
    else:
        app.pop("launch", None)
    app.pop("notice", None)
    write_launcher(app, os.path.expanduser(settings["install_dir"]), folder)
    return app.get("launch")


def steam_target(app):
    """(Exe, StartDir, needs Proton) for a Steam shortcut, or None when there is nothing to start.
    A Flatpak starts through flatpak itself, with steam_args(app) as its launch options."""
    p = (app.get("installed") or {}).get("path") or ""
    if is_flatpak(app):
        return (shutil.which("flatpak") or "/usr/bin/flatpak", str(Path.home()), False) if os.path.isdir(p) else None
    if os.path.isfile(p):
        return p, os.path.dirname(p), False
    if not os.path.isdir(p):
        return None
    lc = app.get("launch") or {}
    if lc.get("kind") == "windows":
        exe = os.path.join(p, lc["file"])
        return (exe, os.path.dirname(exe), True) if os.path.isfile(exe) else None
    lnch = app.get("launcher")
    return (lnch, p, False) if lnch and os.path.isfile(lnch) else None


def steam_args(app):
    """Launch options for the Steam shortcut: only a Flatpak needs any."""
    return "run %s" % app["installed"]["flatpak_id"] if is_flatpak(app) else ""


def prune_stale(dest, old, new):
    """Delete files the previous release shipped and the new one doesn't (and folders left empty).
    Only paths from the old manifest are touched, so saves and configs stay."""
    keep = set(new)
    keep_dirs = {d for rel in new for d in _parents(rel)}
    gone = set()
    for rel in old:
        p = os.path.join(dest, rel)
        if rel in keep or not _inside(dest, os.path.dirname(p)) or not (os.path.islink(p) or os.path.isfile(p)):
            continue
        os.remove(p)
        gone.update(_parents(rel))
    for d in sorted(gone - keep_dirs, key=len, reverse=True):
        try:
            os.rmdir(os.path.join(dest, d))
        except OSError:
            pass


def _parents(rel):
    out, d = [], os.path.dirname(rel)
    while d:
        out.append(d)
        d = os.path.dirname(d)
    return out


FLATPAK_REF = re.compile(rb"app/([A-Za-z_][\w-]*(?:\.[\w-]+){2,})/(?:x86_64|aarch64|i386|arm)/([\w.-]+)")


def flatpak_bundle_ref(path):
    """(app id, branch) of a .flatpak bundle: its header keeps the ref as plain text."""
    with open(path, "rb") as f:
        m = FLATPAK_REF.search(f.read(1 << 20))
    return (m.group(1).decode(), m.group(2).decode()) if m else (None, None)


def flatpak_dir(app_id):
    """Where a --user Flatpak lives; it exists exactly while the app is installed."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return str(data / "flatpak" / "app" / app_id)


def flatpak_cmd(*args, timeout=900):
    if not shutil.which("flatpak"):
        raise RuntimeError("Flatpak is not installed on this system")
    r = subprocess.run(["flatpak"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError("flatpak %s failed: %s" % (args[0], r.stdout.strip()[-500:] or "exit %d" % r.returncode))
    return r.stdout


def is_flatpak(app):
    return (app.get("installed") or {}).get("kind") == "flatpak"


def install_app(app, settings, progress=None, phase=None):
    latest = app.get("latest") or {}
    asset = latest.get("asset")
    if not asset:
        raise RuntimeError(app.get("error") or "Nothing to install, run a check first")

    install_dir = Path(os.path.expanduser(settings["install_dir"]))
    install_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(app["name"])
    pinned = pinned_path(app)
    old_files = (app.get("installed") or {}).get("files")
    manifest = None
    work = Path(tempfile.mkdtemp(prefix=".sedotdeck-", dir=install_dir))
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

        if kind == "flatpak":
            flatpak_id, _branch = flatpak_bundle_ref(dl)
            if not flatpak_id:
                raise RuntimeError("Couldn't read the app ID from %s, so it can't be installed" % asset["name"])
            if phase:
                phase("Installing")
            # --reinstall replaces the old version; the app's data in ~/.var/app stays.
            flatpak_cmd("install", "--user", "--noninteractive", "-y", "--reinstall", "--bundle", str(dl))
            path = flatpak_dir(flatpak_id)

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
                manifest = sorted(os.path.relpath(os.path.join(r, f), root) for r, _d, fs in os.walk(root) for f in fs)
                copy_over(str(root), str(dest_dir))
                path = str(dest_dir)
                if old_files and (app.get("installed") or {}).get("path") == path:
                    prune_stale(path, old_files, manifest)

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
    if kind == "flatpak":
        app["installed"]["flatpak_id"] = flatpak_id
    app["error"] = ""
    app.pop("notice", None)
    if manifest is not None:
        app["installed"]["files"] = manifest
        try:
            write_launcher(app, str(install_dir), path)
        except OSError:
            pass
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
    """AppImages already in the install folder that SedotDeck doesn't manage yet, best match first."""
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
    if is_flatpak(app):
        raise RuntimeError("Flatpak apps have no backup to roll back to")
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
    if delete_files and is_flatpak(app):
        if os.path.isdir(app["installed"]["path"]):
            flatpak_cmd("uninstall", "--user", "--noninteractive", "-y", app["installed"]["flatpak_id"])
    elif delete_files:
        if app.get("launcher") and os.path.isfile(app["launcher"]):
            os.remove(app["launcher"])
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


def apply_options(app, opts):
    """Per-app options from the UI or an import. Validates before changing anything."""
    o = {}
    for k in ("pattern", "skip_titles"):
        if k in opts:
            v = opts[k] or ""
            if not isinstance(v, str):
                raise ValueError("%s must be text" % k)
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError("Invalid %s (%s)" % ("asset pattern" if k == "pattern" else "skip filter", e))
            o[k] = v
    for k in ("prerelease", "latest_only"):
        if k in opts:
            o[k] = bool(opts[k])
    if "category" in opts:
        c = opts["category"] or ""
        if not isinstance(c, str):
            raise ValueError("category must be text")
        o["category"] = " ".join(c.split())[:40]
    for k, v in o.items():
        if v in ("", False) and k not in ("pattern", "prerelease"):
            app.pop(k, None)
        else:
            app[k] = v


EXPORT_FORMAT = "sedotdeck-apps"
LEGACY_EXPORT_FORMATS = ("deckport-apps",)
EXPORT_FIELDS = ("name", "provider", "host", "repo", "pattern", "prerelease", "skip_titles", "latest_only", "category")


def export_apps(cfg):
    """App list to share or back up: no tokens, local paths or install state."""
    return {
        "format": EXPORT_FORMAT,
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": [{k: a[k] for k in EXPORT_FIELDS if k in a} for a in cfg["apps"]],
    }


def import_apps(cfg, data):
    """Merge an export into cfg. Validates everything first, so a bad file changes nothing."""
    if not isinstance(data, dict) or data.get("format") not in (EXPORT_FORMAT,) + LEGACY_EXPORT_FORMATS or not isinstance(data.get("apps"), list):
        raise ValueError("This is not a SedotDeck export file")
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
        app = new_app("", name[:100], pattern, bool(item.get("prerelease")), src)
        try:
            apply_options(app, {k: item[k] for k in ("skip_titles", "latest_only", "category") if k in item})
        except ValueError as e:
            raise ValueError("Entry %d: %s" % (i, e))
        incoming.append(app)
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
    if is_flatpak(app):
        subprocess.Popen(["flatpak", "run", inst["flatpak_id"]], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    elif os.path.isdir(p):
        launcher = app.get("launcher")
        if (app.get("launch") or {}).get("kind") == "windows":
            raise RuntimeError("%s is a Windows program: add it to Steam and play it there with Proton" % app["name"])
        if launcher and os.path.isfile(launcher):
            subprocess.Popen([launcher], cwd=p, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            open_path(p)
    else:
        subprocess.Popen([p], cwd=os.path.dirname(p), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


# --------------------------------------------------------------------------- folder scan

def _src_key(provider, host, repo):
    return (provider or "", (host or "").lower(), (repo or "").lower())


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
    """The repo an AppImage on disk came from, read from its embedded update info, or ""."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _ident_cache:
        _ident_cache[key] = source_from_update_info(read_update_info(path))
    return _ident_cache[key]


def scan_folder(cfg):
    """AppImages in the install folder that SedotDeck doesn't manage yet, with the repo each one
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
        if (not f.lower().endswith(".appimage") or f.lower().startswith("sedotdeck") or os.path.islink(p)
                or not os.path.isfile(p) or os.path.realpath(p) in taken or p in ignored):
            continue
        try:
            src = identify_file(p)
        except OSError:
            continue
        item = {"path": p, "file": f, "size": os.path.getsize(p), "src": src, "name": ""}
        if src:
            prov, host, repo = parse_source(src)
            item["display"] = repo if host == "github.com" else "%s/%s" % (host, repo)
            item["name"] = repo.split("/")[-1]
            owner = by_src.get(_src_key(prov or "", host, repo))
            if owner:
                inst = owner.get("installed") or {}
                if inst.get("path") and os.path.exists(inst["path"]):
                    continue  # a spare copy of an app SedotDeck already keeps up to date
                item["app_id"], item["name"] = owner["id"], owner["name"]
        out.append(item)
    return out


# --------------------------------------------------------------------------- GitHub search

def github_search(query, token="", tracked=(), sort="best"):
    """Repositories whose name or description match query, so a port can be found by the game's name.
    Sort is "best" (GitHub's best match), "stars" or "updated": stars alone surfaces spam repos that mention every game."""
    query = " ".join(str(query or "").split())[:200]
    if len(query) < 2:
        raise ValueError("Type at least two characters to search")
    params = {"q": query + " in:name,description fork:false", "per_page": 30}
    if sort in ("stars", "updated"):
        params.update(sort=sort, order="desc")
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(params)
    data = http_json(url, token, "github")
    tracked = {t.lower() for t in tracked}
    out = []
    for r in data.get("items") or []:
        repo = str(r.get("full_name") or "")
        if not valid_repo(repo):
            continue
        out.append({
            "repo": repo, "name": str(r.get("name") or repo), "description": str(r.get("description") or "")[:300],
            "stars": int(r.get("stargazers_count") or 0), "language": str(r.get("language") or ""),
            "pushed_at": str(r.get("pushed_at") or ""), "archived": bool(r.get("archived")),
            "url": str(r.get("html_url") or "https://github.com/" + repo), "tracked": repo.lower() in tracked,
        })
    return {"items": out, "total": int(data.get("total_count") or 0)}


# --------------------------------------------------------------------------- SedotDeck's own updates

SELF_REPO = "intermission88/SedotDeck"
SELF_CACHE = CONFIG_DIR / "self-update.json"
SELF_MAX_AGE = 6 * 3600
SELF_APPIMAGE = re.compile(r"SedotDeck(-.+)?-x86_64\.AppImage$")


def version_key(text):
    """Sortable key for "v1.2.3" or "0.0.1-alpha.2"; a pre-release sorts before its release."""
    m = re.match(r"v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$", str(text or "").strip())
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    pre = m.group(2)
    tail = (1,) if not pre else (0,) + tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))
    return nums + (0,) * (4 - len(nums)), tail


def self_install_method():
    """How this copy can update itself: "appimage", "script", "decky", or "manual" (a git checkout, say)."""
    appimage = os.environ.get("APPIMAGE") or ""
    if appimage and os.path.isfile(appimage) and os.access(os.path.dirname(appimage), os.W_OK):
        return "appimage"
    if "decky" in sys.modules:
        return "decky"
    here = Path(os.path.abspath(__file__))
    if (here.parent / ".git").exists() or not os.access(here.parent, os.W_OK):
        return "manual"
    return "script"


def self_update_check(token="", refresh=False):
    """{"current", "latest", "newer", "notes", "url", "method"}: the newest SedotDeck release, cached for 6 hours."""
    cache = None
    try:
        cache = json.loads(SELF_CACHE.read_text())
    except (OSError, ValueError):
        pass
    if not cache or refresh or time.time() - cache.get("fetched_at", 0) > SELF_MAX_AGE:
        rels = http_json("https://api.github.com/repos/%s/releases?per_page=20" % SELF_REPO, token, "github")
        rels = [_norm_github(r) for r in rels if not r.get("draft") and version_key(r.get("tag_name"))]
        best = max(rels, key=lambda r: version_key(r["tag_name"]), default=None)
        cache = {"fetched_at": time.time(), "release": best}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SELF_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, SELF_CACHE)
    rel = cache.get("release") or {}
    tag = rel.get("tag_name") or ""
    newer = bool(tag) and version_key(tag) > version_key(VERSION)
    return {"current": VERSION, "latest": tag, "newer": newer, "notes": (rel.get("body") or "")[:NOTES_MAX],
            "url": rel.get("html_url") or "https://github.com/%s/releases" % SELF_REPO,
            "published": rel.get("published_at") or "", "method": self_install_method()}


def self_update(token="", progress=None):
    """Replace this AppImage or script with the newest release. Takes effect on the next start."""
    method = self_install_method()
    cache = json.loads(SELF_CACHE.read_text()) if SELF_CACHE.exists() else {}
    rel = cache.get("release") or {}
    tag = rel.get("tag_name") or ""
    if not tag or version_key(tag) <= version_key(VERSION):
        raise RuntimeError("SedotDeck is up to date")
    if method == "appimage":
        target = os.environ["APPIMAGE"]
        asset = next((a for a in rel.get("assets") or [] if SELF_APPIMAGE.search(a["name"])), None)
        if not asset:
            raise RuntimeError("Release %s has no AppImage" % tag)
        url, expected = asset["browser_download_url"], asset.get("sha256") or ""
    elif method == "script":
        target = os.path.abspath(__file__)
        url = "https://raw.githubusercontent.com/%s/%s/sedotdeck.py" % (SELF_REPO, urllib.parse.quote(tag))
        expected = ""
    else:
        raise RuntimeError("This copy of SedotDeck can't update itself. Download %s from the Releases page." % tag)
    tmp = target + ".download"
    try:
        actual = download(url, tmp, progress)
        if expected and actual != expected:
            raise RuntimeError("SHA-256 mismatch for the %s download, so it was deleted" % tag)
        with open(tmp, "rb") as f:
            head = f.read(64)
        if method == "appimage" and head[:4] != b"\x7fELF" or method == "script" and b"python" not in head:
            raise RuntimeError("The %s download isn't a SedotDeck %s" % (tag, "AppImage" if method == "appimage" else "script"))
        make_executable(tmp)
        os.replace(tmp, target)  # the running copy keeps its open inode, so this is safe
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return tag


# --------------------------------------------------------------------------- Recomp Haven

# Recomp Haven (recomphaven.com, by rikuwru) tracks game recompilations and PC ports. Its list is
# read from the site's public data file and kept for a day; SedotDeck shows the ones with a Linux release.
RH_SITE = "https://recomphaven.com/"
RH_LOGO = RH_SITE + "assets/icons/icon-192.png"
RH_DATA_URL = "https://raw.githubusercontent.com/rikuwru/recomphaven/main/data/recomps.js"
RH_TABLES_URL = "https://raw.githubusercontent.com/rikuwru/recomphaven/main/data/tables.js"
RH_CACHE = CONFIG_DIR / "recomphaven.json"
RH_MAX_AGE = 24 * 3600
RH_TIERS = "SABCD"


def parse_recomphaven(text):
    """Entries SedotDeck can install from Recomp Haven's data/recomps.js, best tier first."""
    start = text.index("[", text.index("window.RECOMPS"))
    items, _end = json.JSONDecoder().raw_decode(text, start)
    out = []
    for e in items:
        if not isinstance(e, dict) or e.get("romFiles") or not e.get("version"):
            continue  # projects that hand out game files are hidden on the site too
        if "linux" not in (e.get("platforms") or []):
            continue
        try:
            provider, host, repo = parse_source(str(e.get("repo") or ""))
        except ValueError:
            continue
        if provider != "github":
            continue
        cover = str(e.get("cover") or "")
        tier = e.get("quality") if e.get("quality") in tuple(RH_TIERS) else ""
        out.append({
            "id": str(e.get("id") or ""), "title": str(e.get("title") or repo), "project": str(e.get("project") or ""),
            "console": str(e.get("console") or ""), "year": e.get("year"), "genre": str(e.get("genre") or ""),
            "stage": str(e.get("stage") or ""), "tier": tier, "repo": repo, "key": ("%s/%s" % (host, repo)).lower(),
            "version": str(e.get("version") or ""), "released": str(e.get("versionDate") or ""),
            "stars": e.get("stars") or 0, "deck": "steam-deck" in (e.get("features") or []),
            "windows": "windows" in (e.get("platforms") or []),
            "requires": str(e.get("requires") or "")[:300], "summary": str(e.get("summary") or "")[:600],
            "cover": RH_SITE + cover if re.fullmatch(r"assets/covers/[\w.-]+\.(webp|png|jpe?g)", cover) else "",
        })
    out.sort(key=lambda x: (RH_TIERS.index(x["tier"]) if x["tier"] else 9, -x["stars"]))
    return out


def parse_recomphaven_consoles(text):
    """{key: {"label", "short", "color"}} from Recomp Haven's data/tables.js."""
    start = text.index("{", text.index("window.RECOMPHAVEN_TABLES"))
    tables, _end = json.JSONDecoder().raw_decode(text, start)
    out = {}
    for k, v in (tables.get("consoles") or {}).items():
        if isinstance(v, dict):
            color = str(v.get("color") or "")
            out[str(k)] = {"label": str(v.get("label") or k), "short": str(v.get("short") or k),
                           "color": color if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color) else ""}
    return out


def _rh_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _opener.open(req, timeout=30) as r:
        return r.read(32 * 1024 * 1024).decode("utf-8")


def recomphaven(refresh=False):
    """{"items", "fetched_at", "site", "stale"}: the cached list, refetched once a day or on request."""
    cache = None
    try:
        cache = json.loads(RH_CACHE.read_text())
    except (OSError, ValueError):
        pass
    if cache and not refresh and time.time() - cache.get("fetched_at", 0) < RH_MAX_AGE:
        return dict(cache, site=RH_SITE, logo=RH_LOGO, stale="")
    try:
        data = {"fetched_at": time.time(), "items": parse_recomphaven(_rh_get(RH_DATA_URL)), "consoles": {}}
    except Exception as e:
        if cache:
            return dict(cache, site=RH_SITE, logo=RH_LOGO, stale="Couldn't refresh (%s), showing the saved list" % e)
        raise RuntimeError("Couldn't load Recomp Haven: %s" % e)
    try:
        data["consoles"] = parse_recomphaven_consoles(_rh_get(RH_TABLES_URL))
    except Exception:
        data["consoles"] = (cache or {}).get("consoles") or {}  # labels are a nicety; keys still show
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RH_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, RH_CACHE)
    return dict(data, site=RH_SITE, logo=RH_LOGO, stale="")


# --------------------------------------------------------------------------- Quiver catalog

# quiverlauncher.com (by Thomas Georgiadis) is the catalog behind Quiver Launcher. Its public API lists the apps
# and, separately, the repository each one releases from; SedotDeck joins the two and keeps the result for a day.
# Quiver's name may be used to say where the list comes from, but not its logo or icons (TRADEMARKS.md in its repo).
QV_SITE = "https://quiverlauncher.com/"
QV_API = "https://api.quiverlauncher.com/api/v1"
QV_CACHE = CONFIG_DIR / "quiver.json"
QV_MAX_AGE = 24 * 3600
QV_HOSTS = {"github": "github.com", "gitlab": "gitlab.com"}


def parse_quiver(apps, statuses):
    """Catalog apps SedotDeck can install (a verified Linux release on GitHub or GitLab), newest first."""
    repos = {}
    for s in statuses:
        if isinstance(s, dict) and s.get("provider") in QV_HOSTS and s.get("repository"):
            repos[str(s.get("slug") or "")] = (s["provider"], str(s["repository"]))
    out = []
    for e in apps:
        if not isinstance(e, dict) or "linux" not in (e.get("supportedOS") or []):
            continue
        slug, verified = str(e.get("slug") or ""), e.get("verified") or {}
        provider, repo = repos.get(slug, ("", ""))
        if not verified.get("version") or not re.fullmatch(r"[\w.-]+(/[\w.-]+)+", repo):
            continue
        art = e.get("libraryArt") or {}
        cover = str(art.get("capsule") or e.get("artwork") or "")
        # SteamGridDB's full grids are ~1 MB PNGs; its thumbnail of the same image is a small JPEG.
        cover = re.sub(r"^https://cdn2\.steamgriddb\.com/grid/(\w+)\.\w+$", r"https://cdn2.steamgriddb.com/thumb/\1.jpg", cover)
        dev = e.get("developer") or {}
        out.append({
            "id": slug, "title": str(e.get("name") or repo)[:200], "project": str(e.get("projectName") or "")[:200],
            "console": str((e.get("consoles") or [""])[0]), "type": str(e.get("projectType") or ""),
            "ai": e.get("aiLevel") if e.get("aiLevel") in ("none", "assisted", "generated") else "",
            "provider": provider, "repo": repo, "key": ("%s/%s" % (QV_HOSTS[provider], repo)).lower(),
            "version": str(verified["version"]), "released": (e.get("lastReleaseAt") or 0) / 1000,
            "added": (e.get("addedAt") or 0) / 1000, "developer": str(dev.get("name") or "")[:120],
            "summary": str(e.get("description") or "")[:600], "recommended": int(e.get("recommended") or 0),
            "broken": int(e.get("reportBroken") or 0),
            "cover": cover if re.fullmatch(r"https://[^\s\"'<>()]+", cover) else "",
        })
    out.sort(key=lambda x: -x["added"])
    return out


def _qv_get(path):
    req = urllib.request.Request(QV_API + path, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _opener.open(req, timeout=30) as r:
        return json.loads(r.read(16 * 1024 * 1024).decode("utf-8"))


def _qv_all(path):
    """Every item of a paged list (100 a page, following nextCursor)."""
    items, cursor = [], None
    for _ in range(50):
        page = _qv_get(path + ("&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else ""))
        items += page.get("items") or []
        cursor = page.get("nextCursor")
        if page.get("isDone", True) or not cursor:
            break
    return items


def _known_stars():
    """{key: stars} from the Recomp Haven and Recomp Launcher lists already saved on disk; nothing is fetched."""
    known = {}
    for path in (RH_CACHE, RL_CACHE):
        try:
            items = json.loads(path.read_text()).get("items")
        except (OSError, ValueError, AttributeError):
            continue
        for x in items if isinstance(items, list) else []:
            if not isinstance(x, dict) or not x.get("key"):
                continue
            try:
                known[x["key"]] = max(int(x.get("stars") or 0), known.get(x["key"], 0))
            except (TypeError, ValueError):
                pass
    return known


def _qv_out(data, stale):
    """The saved Quiver list with the stars another catalog knows for each repository (0 when none)."""
    known = _known_stars()
    items = [dict(x, stars=known.get(x.get("key"), 0)) for x in data.get("items") or []]
    return dict(data, items=items, site=QV_SITE, stale=stale)


def quiver(refresh=False):
    """{"items", "consoles", "fetched_at", "site", "stale"}: the cached catalog, refetched once a day or on request."""
    cache = None
    try:
        cache = json.loads(QV_CACHE.read_text())
    except (OSError, ValueError):
        pass
    if cache and not refresh and time.time() - cache.get("fetched_at", 0) < QV_MAX_AGE:
        return _qv_out(cache, "")
    try:
        items = parse_quiver(_qv_all("/apps?limit=100&os=linux&sort=added"), _qv_all("/release-status?limit=100"))
        data = {"fetched_at": time.time(), "items": items, "consoles": {}}
    except Exception as e:
        if cache:
            return _qv_out(cache, "Couldn't refresh (%s), showing the saved list" % e)
        raise RuntimeError("Couldn't load the Quiver catalog: %s" % e)
    try:
        data["consoles"] = {str(c["id"]): str(c.get("name") or c["id"]) for c in _qv_get("/facets").get("consoles") or []
                            if isinstance(c, dict) and c.get("id")}
    except Exception:
        data["consoles"] = (cache or {}).get("consoles") or {}  # labels are a nicety; ids still show
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QV_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, QV_CACHE)
    return _qv_out(data, "")


# --------------------------------------------------------------------------- Recomp Launcher catalog

# recomplauncher.com (RecompLauncher) lists PC recomps from each developer's GitHub; SedotDeck reads its public
# pc-catalog.json and keeps it for a day. Its logo isn't used.
RL_SITE = "https://recomplauncher.com/"
RL_DATA_URL = RL_SITE + "pc-catalog.json"
RL_CACHE = CONFIG_DIR / "recomplauncher.json"
RL_MAX_AGE = 24 * 3600


def _rl_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_recomplauncher(data):
    """Entries SedotDeck can install from the catalog (a Linux release on GitHub), most stars first."""
    out = []
    for e in data.get("apps") or []:
        if not isinstance(e, dict) or "linux" not in (e.get("systems") or []) or e.get("soon") or not e.get("version"):
            continue
        repo = str(e.get("repo") or "")
        if not valid_repo(repo):
            continue
        cover = str(e.get("cover") or "")
        out.append({
            "id": str(e.get("id") or ""), "title": str(e.get("name") or repo)[:200], "game": str(e.get("game") or "")[:200],
            "console": str(e.get("origin") or ""), "repo": repo, "key": ("github.com/" + repo).lower(),
            "version": str(e["version"]), "released": str(e.get("released") or ""), "added": str(e.get("added") or ""),
            "stars": _rl_int(e.get("stars")), "downloads": _rl_int(e.get("downloads")),
            "developer": str(e.get("author") or "")[:120], "summary": str(e.get("blurb") or "")[:600],
            "needs": str(e.get("needs") or "")[:300],
            "cover": cover if re.fullmatch(r"https://[^\s\"'<>()]+", cover) else "",
        })
    out.sort(key=lambda x: -x["stars"])
    return out


def _rl_get():
    req = urllib.request.Request(RL_DATA_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _opener.open(req, timeout=30) as r:
        return json.loads(r.read(16 * 1024 * 1024).decode("utf-8"))


def recomplauncher(refresh=False):
    """{"items", "consoles", "fetched_at", "site", "stale"}: the cached catalog, refetched once a day or on request."""
    cache = None
    try:
        cache = json.loads(RL_CACHE.read_text())
    except (OSError, ValueError):
        pass
    if cache and not refresh and time.time() - cache.get("fetched_at", 0) < RL_MAX_AGE:
        return dict(cache, site=RL_SITE, stale="")
    try:
        raw = _rl_get()
        data = {"fetched_at": time.time(), "items": parse_recomplauncher(raw),
                "consoles": {str(o["id"]): str(o.get("name") or o["id"]) for o in raw.get("origins") or []
                             if isinstance(o, dict) and o.get("id")}}
    except Exception as e:
        if cache:
            return dict(cache, site=RL_SITE, stale="Couldn't refresh (%s), showing the saved list" % e)
        raise RuntimeError("Couldn't load the Recomp Launcher catalog: %s" % e)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RL_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, RL_CACHE)
    return dict(data, site=RL_SITE, stale="")


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


def _shortcut_key(entry):
    """What a shortcut starts: its exe, plus launch options when it has any (Flatpaks share one exe)."""
    opts = next((v.strip() for t, k, v in entry if t == 1 and k.lower() == "launchoptions"), "")
    return _shortcut_exe(entry) + (" " + opts if opts else "")


def steam_key(app, target=None):
    target = target or steam_target(app)
    return target[0] + (" " + steam_args(app) if steam_args(app) else "") if target else ""


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
            exes = {k for t, _k, e in _shortcut_list(vdf_load(f.read_bytes())) if t == 0
                    for k in (_shortcut_exe(e), _shortcut_key(e))}
        except (OSError, ValueError, IndexError, struct.error):
            exes = set()
        _steam_cache.update(key=(str(f), m), exes=exes)
    return _steam_cache["exes"]


def add_to_steam(app, shortcuts=None):
    """Add the installed AppImage (or a folder port's launcher or .exe) as a Non-Steam game. Returns False if it
    was already there. A Windows .exe still needs Proton turned on in its Steam properties (the Decky plugin does that)."""
    inst_path = (app.get("installed") or {}).get("path") or ""
    target = steam_target(app)
    if not target:
        if os.path.isdir(inst_path):
            raise RuntimeError("%s has no program to start yet. Use Choose program first." % app["name"])
        raise RuntimeError("Install %s first" % app["name"])
    path, start, _proton = target
    f = shortcuts or steam_shortcuts_file()
    if not f:
        raise RuntimeError("Steam not found. Start Steam once and log in, then try again.")
    if shortcuts is None and steam_running():
        raise RuntimeError("Close Steam first (Steam menu > Exit), then press Add to Steam again. "
                           "In Game Mode, use the SedotDeck Decky plugin instead.")
    f = Path(f)
    data = f.read_bytes() if f.exists() else b""
    root = vdf_load(data) if data else []
    lst = _shortcut_list(root)
    key = steam_key(app, target)
    if any(_shortcut_key(e) == key for t, _k, e in lst if t == 0):
        return False
    exe, name = '"%s"' % path, app["name"]
    appid = (zlib.crc32((exe + name).encode()) | 0x80000000) - (1 << 32)  # stored as a signed int32
    nxt = max([int(k) for _t, k, _v in lst if k.isdigit()] + [-1]) + 1
    lst.append((0, str(nxt), [
        (2, "appid", appid), (1, "AppName", name), (1, "Exe", exe),
        (1, "StartDir", '"%s"' % start), (1, "icon", ""), (1, "ShortcutPath", ""),
        (1, "LaunchOptions", steam_args(app)), (2, "IsHidden", 0), (2, "AllowDesktopConfig", 1), (2, "AllowOverlay", 1),
        (2, "OpenVR", 0), (2, "Devkit", 0), (1, "DevkitGameID", ""), (2, "DevkitOverrideAppID", 0),
        (2, "LastPlayTime", 0), (1, "FlatpakAppID", ""), (0, "tags", []),
    ]))
    f.parent.mkdir(parents=True, exist_ok=True)
    if data:
        shutil.copy2(f, str(f) + ".sedotdeck.bak")  # the previous version, in case Steam dislikes ours
    tmp = f.with_suffix(".vdf.tmp")
    tmp.write_bytes(vdf_dump(root))
    os.replace(tmp, f)
    return True


def rename_app_file(app, shortcuts=None):
    """Give an AppImage the clean name <App>.AppImage (and its .old), repointing a Steam shortcut to it."""
    inst = app.get("installed") or {}
    old = inst.get("path") or ""
    if inst.get("kind") != "appimage" or not os.path.isfile(old):
        raise RuntimeError("%s has no AppImage file to rename" % app["name"])
    new = os.path.join(os.path.dirname(old), safe_name(app["name"]) + ".AppImage")
    if new == old:
        raise RuntimeError("%s already has that name" % os.path.basename(old))
    if os.path.lexists(new):
        raise RuntimeError("%s already exists, so %s keeps its name" % (os.path.basename(new), os.path.basename(old)))
    f = shortcuts or steam_shortcuts_file()
    f = Path(f) if f else None
    root, hits, data = None, [], b""
    if f and f.exists():
        data = f.read_bytes()
        try:
            root = vdf_load(data) if data else []
        except (ValueError, IndexError, struct.error):
            raise RuntimeError("Can't read Steam's shortcuts file, so %s can't be renamed safely" % os.path.basename(old))
        hits = [e for t, _k, e in _shortcut_list(root) if t == 0 and _shortcut_exe(e) == old]
    if hits and shortcuts is None and steam_running():
        raise RuntimeError("Close Steam first (Steam menu > Exit), then rename: a Steam shortcut points at this file.")
    for e in hits:
        for i, (t, k, v) in enumerate(e):
            if t == 1 and k.lower() == "exe":
                e[i] = (t, k, '"%s"' % new)
            elif t == 1 and k.lower() == "startdir" and v.strip().strip('"') in (old, os.path.dirname(old)):
                e[i] = (t, k, '"%s"' % (new if v.strip().strip('"') == old else os.path.dirname(new)))
    os.replace(old, new)
    if os.path.exists(old + ".old"):
        os.replace(old + ".old", new + ".old")
    if hits:
        try:
            shutil.copy2(f, str(f) + ".sedotdeck.bak")
            tmp = f.with_suffix(".vdf.tmp")
            tmp.write_bytes(vdf_dump(root))
            os.replace(tmp, f)
        except OSError:
            os.replace(new, old)
            if os.path.exists(new + ".old"):
                os.replace(new + ".old", old + ".old")
            raise
    app["target_path"] = inst["path"] = new
    app.pop("notice", None)
    return new


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
        """Pick up changes saved by another SedotDeck (desktop UI, CLI, Decky plugin) while idle."""
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
                tgt = steam_target(a)
                d["has_launcher"] = bool(os.path.isdir(p) and tgt and not tgt[2] and not is_flatpak(a))
                d["steam_exe"], d["steam_dir"], d["proton"] = tgt or ("", "", False)
                d["steam_args"] = steam_args(a) if tgt else ""
                d["in_steam"] = bool(tgt) and steam_key(a, tgt) in in_steam
                clean = safe_name(a["name"]) + ".AppImage"
                d["rename_to"] = clean if d["exists"] and inst.get("kind") == "appimage" and os.path.basename(p) != clean else ""
                d["disk"] = (os.path.getsize(p) if os.path.isfile(p) else inst.get("size", 0)) if d["exists"] else 0
                d["old_disk"] = os.path.getsize(p + ".old") if d["has_backup"] else 0
                apps.append(d)
            s = dict(self.cfg["settings"])
            s["has_token"] = bool(s.pop("github_token", ""))
            s["scan_ignored"] = len(s.get("scan_ignored") or [])
            s["install_dir_short"] = short_home(os.path.expanduser(s["install_dir"]))
            return {"apps": apps, "settings": s, "jobs": self.jobs.snapshot(), "version": VERSION,
                    "found": scan_folder(self.cfg), "steam": bool(steam_shortcuts_file()),
                    "steam_running": steam_running(max_age=10)}

    def check(self, app):
        def fn(job):
            check_app(app, self.gh_token())
            self.save()
            if app.get("error"):
                raise RuntimeError(app["error"])
        return self.jobs.start(app["id"], "Checking", fn)

    def adopt(self, app, path, rename=False):
        def fn(job):
            check_app(app, self.gh_token())
            if app.get("error"):
                raise RuntimeError(app["error"])
            adopt_app(app, path)
            self.save()
            if rename and os.path.basename(path) != safe_name(app["name"]) + ".AppImage":
                try:
                    with _lock:
                        rename_app_file(app)
                except (OSError, RuntimeError) as e:
                    app["notice"] = "Kept the file name: %s" % e  # adoption itself worked
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
            if body.get("skip_titles"):
                re.compile(body["skip_titles"])
            rel = fetch_release(src, body.get("prerelease", False),
                                self.gh_token() if src["provider"] == "github" else "",
                                body.get("skip_titles", ""), bool(body.get("latest_only")))
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
                self.adopt(app, path_, body.get("rename", False))
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
            apply_options(app, body)
            if body.get("rh_id"):
                app["rh_id"] = str(body["rh_id"])[:120]
            if body.get("qv_id"):
                app["qv_id"] = str(body["qv_id"])[:120]
            if body.get("rl_id"):
                app["rl_id"] = str(body["rl_id"])[:120]
            with _lock:
                if any(same_source(a, app) for a in cfg["apps"]):
                    raise ValueError("%s is already tracked" % display_repo(app))
                adopt = validate_adopt_path(cfg, body["adopt_path"]) if body.get("adopt_path") else None
                if adopt:
                    app["target_path"] = adopt  # claim the file now so the folder scan stops offering it
                cfg["apps"].append(app)
                self.save()
            if adopt:
                self.adopt(app, adopt, body.get("rename", False))
            else:
                self.check(app)
            return {"ok": True, "id": app["id"]}

        if path == "/api/edit":
            app = self._app(body)
            with _lock:
                if "name" in body and body["name"].strip():
                    app["name"] = body["name"].strip()
                apply_options(app, body)
                adopt = validate_adopt_path(cfg, body["adopt_path"], app["id"]) if body.get("adopt_path") else None
                if adopt:
                    app["target_path"] = adopt
                app["latest"] = None
                self.save()
            if adopt:
                self.adopt(app, adopt, body.get("rename", False))
            else:
                self.check(app)
            return {"ok": True}

        if path == "/api/check":
            targets = self._apps(body) if body.get("id") or body.get("ids") else list(cfg["apps"])
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
            elif body.get("ids"):
                targets = [a for a in self._apps(body) if has_update(a, honor_skip=False)]
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
            return {"ok": True, "added": added, "proton": bool((steam_target(app) or ("", "", False))[2])}

        if path == "/api/launch_choices":
            app = self._app(body)
            folder = (app.get("installed") or {}).get("path") or ""
            if not os.path.isdir(folder) or is_flatpak(app):
                raise ValueError("%s is not installed as a folder" % app["name"])
            lc = app.get("launch") or {}
            return {"ok": True, "manual": bool(lc.get("manual")), "current": lc.get("file", ""),
                    "choices": [{k: c[k] for k in ("file", "kind", "size")} for c in launch_candidates(folder, _app_wants(app))[:200]]}

        if path == "/api/set_launch":
            app = self._app(body)
            if (self.jobs.snapshot().get(app["id"]) or {}).get("state") == "running":
                raise ValueError("Wait for %s to finish first" % app["name"])
            with _lock:
                lc = set_launch_file(app, cfg["settings"], str(body.get("file") or ""))
                self.save()
            exe, start, proton = steam_target(app) or ("", "", False)
            return {"ok": True, "launch": lc, "steam_exe": exe, "steam_dir": start, "proton": proton}

        if path == "/api/rename":
            app = self._app(body)
            if (self.jobs.snapshot().get(app["id"]) or {}).get("state") == "running":
                raise ValueError("Wait for %s to finish first" % app["name"])
            with _lock:
                new = rename_app_file(app)
                self.save()
            return {"ok": True, "file": os.path.basename(new)}

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
            targets = self._apps(body)
            running = [a["name"] for a in targets if (self.jobs.snapshot().get(a["id"]) or {}).get("state") == "running"]
            if running:
                raise ValueError("Wait for %s to finish first" % ", ".join(running))
            with _lock:
                for app in targets:
                    remove_app(cfg, app, body.get("delete_files", False))
                self.save()
            return {"ok": True, "count": len(targets)}

        if path == "/api/category":
            targets = self._apps(body)
            with _lock:
                for app in targets:
                    apply_options(app, {"category": body.get("category", "")})
                self.save()
            return {"ok": True, "count": len(targets)}

        if path == "/api/move":
            app = self._app(body)
            new = app.get("moved_to")
            if not new:
                raise ValueError("%s hasn't moved" % app["name"])
            with _lock:
                moved = dict(app, repo=new)
                if any(a["id"] != app["id"] and same_source(a, moved) for a in cfg["apps"]):
                    raise ValueError("%s is already tracked" % display_repo(moved))
                app["repo"] = new
                app.pop("moved_to", None)
                self.save()
            return {"ok": True, "repo": display_repo(app)}

        if path == "/api/recomphaven":
            return recomphaven(bool(body.get("refresh")))

        if path == "/api/quiver":
            return quiver(bool(body.get("refresh")))

        if path == "/api/recomplauncher":
            return recomplauncher(bool(body.get("refresh")))

        if path == "/api/github_search":
            tracked = [a["repo"] for a in cfg["apps"] if a.get("provider") == "github"]
            return github_search(body.get("q"), self.gh_token(), tracked, str(body.get("sort") or "best"))

        if path == "/api/self_update_check":
            info = self_update_check(self.gh_token(), bool(body.get("refresh")))
            info["skipped"] = info["latest"] == cfg["settings"].get("self_update_skip")
            return info

        if path == "/api/self_update_skip":
            with _lock:
                cfg["settings"]["self_update_skip"] = str(body.get("tag") or "")
                self.save()
            return {"ok": True}

        if path == "/api/self_update":
            def fn(job):
                def prog(done, total):
                    job["progress"] = (done / total) if total else 0
                    job["done"] = done
                    job["total"] = total
                job["message"] = "Installed %s. Restart SedotDeck to use it." % self_update(self.gh_token(), prog)
            if not self.jobs.start("__self__", "Updating SedotDeck", fn):
                raise ValueError("SedotDeck is already updating")
            return {"ok": True}

        if path == "/api/launch":
            launch_app(self._app(body))
            return {"ok": True}

        if path == "/api/open_folder":
            p = (self._app(body).get("installed") or {}).get("path") or ""
            if not os.path.isdir(p):
                raise ValueError("Not installed")
            open_path(p)
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
            stem = "sedotdeck-apps-" + data["exported_at"][:10]
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

    def _apps(self, body):
        """The apps named by "ids" (a list), or by "id"."""
        ids = body.get("ids") if isinstance(body.get("ids"), list) else [body.get("id", "")]
        apps = [a for a in self.cfg["apps"] if a["id"] in ids]
        if not apps:
            raise LookupError("app not found")
        return apps


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
            if self.headers.get("X-SedotDeck-Token") != srv.token:
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
        flags = ["--app=" + url, "--class=SedotDeck", "--window-size=1100,760"]
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
        print("SedotDeck is already running at", url)
        if not args.no_browser:
            open_window(url, args.window)
        return
    httpd_ref[0] = httpd
    # The desktop app (Tauri) waits for this exact line to learn the URL.
    print("SEDOTDECK_READY " + url, flush=True)
    print("SedotDeck running at", url, "(Ctrl+C to stop)", flush=True)
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
            print("Run: sedotdeck.py update %s" % safe_name(app["name"]))
        return
    save_config(cfg)
    found = [c for c in find_adopt_candidates(cfg, app["name"], app["repo"], asset, app["id"]) if c["suggested"]]
    if found:
        print("You already have %s. To update that file instead of creating %s.AppImage:"
              % (found[0]["name"], safe_name(app["name"])))
        print("  sedotdeck.py remove %s && sedotdeck.py add %s --adopt %s"
              % (safe_name(app["name"]), args.repo, shlex.quote(found[0]["path"])))
    print("Run: sedotdeck.py update %s" % safe_name(app["name"]))


def cmd_list(args):
    cfg = load_config()
    if not cfg["apps"]:
        print("No apps tracked yet. Add one: sedotdeck.py add https://github.com/owner/repo")
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


def cmd_rename(args):
    cfg = load_config()
    a = find_app(cfg, args.name)
    if not a:
        sys.exit("Unknown app: %s" % args.name)
    try:
        new = rename_app_file(a)
    except RuntimeError as e:
        sys.exit(str(e))
    save_config(cfg)
    print("Renamed to", new)


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
            print("\nRun: sedotdeck.py scan --track   to keep the %d recognized file(s) up to date" % len(known))
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
        print("Run: sedotdeck.py check")


def main():
    p = argparse.ArgumentParser(prog="sedotdeck", description="Update AppImages & PC ports from GitHub Releases")
    p.add_argument("--version", action="version", version="SedotDeck " + VERSION)
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
    s = sub.add_parser("scan", help="find AppImages you already have that SedotDeck can update")
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

    s = sub.add_parser("rename", help="give an AppImage the clean name <App>.AppImage (updates its Steam shortcut)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_rename)

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
<title>SedotDeck</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 256 256%22%3E%3Ctitle%3ESedotDeck%3C/title%3E%3Crect width=%22256%22 height=%22256%22 rx=%2248%22 fill=%22%231a1d23%22/%3E%3Cg transform=%22translate%2815.36 9.36%29 scale%280.88%29%22%3E%3Cpath fill=%22%23e6e8eb%22 fill-rule=%22evenodd%22 d=%22M60 120H196A44 44 0 0 1 240 164V176A44 44 0 0 1 196 220H60A44 44 0 0 1 16 176V164A44 44 0 0 1 60 120ZM84 140H172A6 6 0 0 1 178 146V194A6 6 0 0 1 172 200H84A6 6 0 0 1 78 194V146A6 6 0 0 1 84 140Z%22/%3E%3Cpath fill=%22%231a9fff%22 d=%22M163 122V96A3 3 0 0 1 163.88 93.88L192.16 65.59A17 17 0 0 0 168.12 41.55L139.84 69.84A37 37 0 0 0 129 96V122Z%22/%3E%3C/g%3E%3C/svg%3E">
<style>
/* Close to SteamOS: neutral greys, flat panels, one blue accent, big focus rings for the trackpad and d-pad. */
:root{
  --bg:#0f1115;--panel:#1a1d23;--panel-hi:#23272e;--line:#2b3038;--line-hi:#3a414c;
  --text:#e6e8eb;--dim:#9aa3ad;--faint:#7d8590;
  --accent:#1a9fff;--accent-btn:#1475c4;--accent-btn-hi:#1268ae;
  --good:#8bd17c;--warn:#e8b84a;--bad:#ff7b7b;
  --r:6px;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;color:var(--text);background:var(--bg);
  font:15px/1.45 "Noto Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.mono{font-family:ui-monospace,"Noto Sans Mono","JetBrains Mono",Menlo,monospace;font-size:13px;word-break:break-all}
[hidden]{display:none!important}

header{position:sticky;top:0;z-index:5;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  padding:10px 20px;background:var(--bg);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;margin-right:auto;min-width:0}
.brand svg{width:30px;height:30px;flex:none}
.brand h1{font-size:18px;margin:0;font-weight:700}
.brand .ver-label{font-size:12px;font-weight:400;color:var(--faint);margin-left:8px}
.brand .sub{font-size:12.5px;color:var(--dim)}

button{font:inherit;font-size:14px;font-weight:500;color:var(--text);cursor:pointer;min-height:40px;padding:8px 16px;
  border-radius:var(--r);border:1px solid var(--line-hi);background:var(--panel-hi);transition:background .12s,border-color .12s}
button:hover:not(:disabled){background:#2c313a;border-color:#4a525e}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,[tabindex]:focus-visible{
  outline:3px solid var(--accent);outline-offset:2px}
button:disabled{opacity:.45;cursor:default}
button.primary{color:#fff;border-color:var(--accent-btn);background:var(--accent-btn)}
button.primary:hover:not(:disabled){background:var(--accent-btn-hi);border-color:var(--accent-btn-hi)}
button.ghost{background:transparent;border-color:transparent}
button.ghost:hover:not(:disabled){background:var(--panel-hi);border-color:var(--line)}
button.danger{color:var(--bad)}
button.icon{width:40px;padding:0;display:grid;place-items:center}
button.icon svg{width:19px;height:19px}
button svg{width:16px;height:16px;vertical-align:-3px}
.spin{animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

main{padding:16px 20px 32px;max-width:1180px;margin:0 auto}
.hint{font-size:13px;color:var(--faint)}
.dim{color:var(--faint)} .ok{color:var(--good)} .err{color:var(--bad);font-size:13px}
.empty{text-align:center;color:var(--dim);padding:48px 24px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r)}
.empty b{display:block;color:var(--text);font-size:17px;margin:0 0 6px}

/* ---- tabs */
.tabs{display:flex;align-items:center;gap:4px;margin:0 0 16px;border-bottom:1px solid var(--line)}
.tabs>button{background:none;border:0;border-radius:0;min-height:44px;padding:8px 14px;color:var(--dim);
  display:flex;align-items:center;gap:8px;border-bottom:3px solid transparent;margin-bottom:-1px}
.tabs>button:hover:not(:disabled){background:none;color:var(--text)}
.tabs>button.on{color:var(--text);border-bottom-color:var(--accent)}
.tabs img{width:20px;height:20px;border-radius:4px}
.tabs-end{margin-left:auto} .tabs-end button{min-height:36px;padding:6px 12px;font-size:13.5px}

/* ---- filter chips (segmented) */
.summary{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.summary:empty{display:none}
.summary button{min-height:34px;padding:4px 12px;font-size:13px;color:var(--dim);background:var(--panel);border-color:var(--line)}
.summary button b{color:var(--text);font-weight:600;margin-left:4px}
.summary button.on{color:var(--text);background:var(--panel-hi);border-color:var(--accent)}

/* ---- My apps: one row per app */
.list{display:flex;flex-direction:column;gap:2px}
.group{margin:18px 2px 6px;font-size:14px;font-weight:600;color:var(--dim)}
.group:first-child{margin-top:0}
.group span{color:var(--faint);font-weight:400;margin-left:6px}
.app{position:relative;display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) auto;gap:6px 16px;align-items:center;
  padding:12px 14px;background:var(--panel);border:1px solid transparent;border-radius:var(--r)}
.app:hover{border-color:var(--line)}
.app:focus-within{border-color:var(--line-hi)}
.app .name{font-weight:600;font-size:16px;word-break:break-word;line-height:1.3}
.app .repo{color:var(--dim);font-size:13px}
.app .note{color:var(--faint);font-size:12.5px;margin-top:2px}
.state{font-size:13px;font-weight:600}
.st-update .state{color:var(--accent)} .st-ok .state{color:var(--good)} .st-new .state{color:var(--warn)}
.st-err .state{color:var(--bad)} .st-busy .state{color:var(--dim)}
.ver{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;font-size:13.5px;color:var(--dim)}
.ver b{color:var(--text);font-weight:600}
.ver a[data-a]{white-space:nowrap;font-size:13px}
.tag{font-size:12px;padding:0 6px;border-radius:4px;color:var(--dim);border:1px solid var(--line-hi);white-space:nowrap;line-height:18px}
.tag.ok{color:var(--good);border-color:#3d5a37}
.actions{display:flex;gap:6px;align-items:center;justify-content:flex-end}
.actions button{min-height:38px;padding:6px 14px;font-size:14px}
.actions .more{width:38px;padding:0}
.job{grid-column:1/-1}
.bar{height:4px;border-radius:2px;overflow:hidden;background:var(--line)}
.bar i{display:block;height:100%;width:100%;transform-origin:left;background:var(--accent);transition:transform .3s ease-out}
.bar.wait i{opacity:.4}
.jobtxt{font-size:12.5px;color:var(--dim);margin-top:4px}
.done-check{color:var(--good);font-size:13px;font-weight:600}
#selfProg{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-height:44px;padding:4px 8px;font-size:12.5px;font-weight:400;color:var(--dim)}
#selfProg .bar{display:block;width:120px;max-width:100%}
.su-prog{display:flex;flex-direction:column;gap:8px}

/* ---- select mode */
body.selecting .app{cursor:pointer;user-select:none;padding-left:46px}
body.selecting .app .actions{pointer-events:none;opacity:.4}
.app .pickbox{display:none}
body.selecting .app .pickbox{display:grid;place-items:center;position:absolute;left:14px;top:50%;margin-top:-11px;width:22px;height:22px;
  border-radius:4px;border:2px solid var(--line-hi);background:var(--bg)}
body.selecting .app.sel{border-color:var(--accent)}
body.selecting .app.sel .pickbox{background:var(--accent-btn);border-color:var(--accent-btn)}
body.selecting .app.sel .pickbox::after{content:"";width:6px;height:11px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.selbar{position:fixed;left:12px;right:12px;bottom:16px;margin:0 auto;width:max-content;z-index:12;display:flex;align-items:center;gap:6px;flex-wrap:wrap;
  justify-content:center;padding:8px 10px 8px 16px;border-radius:var(--r);border:1px solid var(--line-hi);background:var(--panel);
  max-width:calc(100vw - 24px);box-shadow:0 8px 24px rgba(0,0,0,.5)}
.selbar button{min-height:38px;padding:6px 12px;font-size:13.5px}
.selbar #selN{font-size:13.5px;margin-right:4px;white-space:nowrap}
.sel-acts{display:flex;gap:6px;flex-wrap:wrap}
body.selecting main{padding-bottom:96px}

/* ---- files found in the install folder */
.found-line{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 14px;margin:0 0 12px;font-size:13.5px;color:var(--dim);
  background:var(--panel);border:1px solid var(--line);border-radius:var(--r)}
.found-line span{flex:1}
.found-line button{min-height:36px;padding:5px 12px;font-size:13px}
.found{padding:14px 16px;margin:0 0 12px;display:flex;flex-direction:column;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r)}
.found h3{margin:0;font-size:15px;font-weight:600}
.found .rows{display:flex;flex-direction:column;gap:2px;max-height:312px;overflow:auto}
.frow{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:4px;color:var(--text);font-size:14px}
label.frow{flex-direction:row;cursor:pointer} label.frow:hover{background:var(--panel-hi)}
.frow input{flex:none} .frow>span{min-width:0;flex:1}
.frow small{display:block;color:var(--faint);font-size:12px}
.frow button{min-height:34px;padding:5px 11px;font-size:13px}
.found .btns{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}

/* ---- dialogs, menu, toast */
dialog{color:var(--text);padding:0;width:min(580px,94vw);max-height:calc(100vh - 32px);overflow:auto;overscroll-behavior:contain;
  border-radius:8px;border:1px solid var(--line-hi);background:var(--panel);box-shadow:0 16px 48px rgba(0,0,0,.6)}
dialog::backdrop{background:rgba(0,0,0,.6)}
.dlg{padding:20px;display:flex;flex-direction:column;gap:12px}
.dlg h2{margin:0;font-size:18px;font-weight:600}
.dlg p{margin:0;color:var(--dim)}
label{font-size:13.5px;color:var(--dim);display:flex;flex-direction:column;gap:6px}
label.row{flex-direction:row;align-items:center;gap:10px;color:var(--text)}
input[type=text],input[type=password],input[type=search],select{font:inherit;color:var(--text);min-height:42px;padding:8px 12px;border-radius:var(--r);
  background:var(--bg);border:1px solid var(--line-hi)}
input[type=text]:focus,input[type=password]:focus,input[type=search]:focus,select:focus{border-color:var(--accent)}
input::placeholder{color:var(--faint)}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent-btn);flex:none}
select{padding-right:8px}
.assets{max-height:168px;overflow:auto;border-radius:var(--r);font-size:13px;background:var(--bg);border:1px solid var(--line)}
.assets div{padding:8px 12px;border-bottom:1px solid var(--line);display:flex;gap:8px;cursor:pointer}
.assets div:last-child{border:0} .assets div:hover{background:var(--panel-hi)}
.assets .pick{background:#1d3a55;color:#fff}
.assets small{margin-left:auto;color:var(--faint)}
.progs{max-height:300px} .progs div{align-items:center} .progs .mono{overflow-wrap:anywhere}
.progs .tag{flex:none}
.dlg .btns{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
/* The last button row stays on screen while a tall dialog scrolls (Deck screens are only 800px high). */
.dlg>.btns:last-child{position:sticky;bottom:0;z-index:1;margin:-4px -20px -20px;padding:14px 20px 20px;
  background:var(--panel);border-top:1px solid var(--line)}
details.adv{border-radius:var(--r);border:1px solid var(--line)}
details.adv>summary{cursor:pointer;padding:10px 14px;font-size:13.5px;color:var(--dim)}
details.adv[open]>summary{border-bottom:1px solid var(--line)}
details.adv>div{padding:12px 14px;display:flex;flex-direction:column;gap:12px}
.using{padding:10px 12px;border-radius:var(--r);border:1px solid #2d5878;background:#16273a;font-size:13.5px}
.notes{white-space:pre-wrap;font-size:13.5px;line-height:1.5;padding:12px 14px;border-radius:var(--r);max-height:340px;overflow:auto;
  background:var(--bg);border:1px solid var(--line);overflow-wrap:anywhere}
.notes b{color:#fff}
.inl{display:flex;gap:8px}.inl input{flex:1;min-width:0}
.menu{position:fixed;z-index:15;min-width:230px;padding:4px;border-radius:var(--r);border:1px solid var(--line-hi);background:var(--panel);
  box-shadow:0 12px 32px rgba(0,0,0,.6);display:flex;flex-direction:column}
.menu button{background:none;border:0;text-align:left;min-height:40px;padding:8px 12px;border-radius:4px;font-size:14px}
.menu button:hover:not(:disabled),.menu button:focus-visible{background:var(--panel-hi)}
.menu button small{display:block;color:var(--faint);font-size:12px}
.menu hr{border:0;border-top:1px solid var(--line);margin:4px 6px}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 16px;border-radius:var(--r);
  border:1px solid var(--line-hi);background:var(--panel-hi);box-shadow:0 8px 24px rgba(0,0,0,.5);
  opacity:0;transition:opacity .15s;pointer-events:none;max-width:90vw;z-index:20}
#toast.show{opacity:1} #toast.bad{border-color:var(--bad)}

/* ---- Discover: GitHub search */
.search{display:flex;gap:8px;margin:0 0 8px}
.search input{flex:1;min-width:0}
#vGh>.hint{margin:0 0 12px}
.repo-row{grid-template-columns:minmax(0,1fr) auto}
.repo-row .desc{color:var(--dim);font-size:13.5px;margin-top:2px}
.meta{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;color:var(--faint);margin-top:4px}
.meta .stars{color:var(--text);font-weight:600}
.added{color:var(--good);font-size:13.5px;font-weight:600}

/* ---- Recomp Haven */
.rh-head{display:flex;gap:12px;align-items:center;margin:0 0 12px}
.rh-head img{width:40px;height:40px;border-radius:8px;flex:none}
.rh-head div{flex:1;min-width:0}
.rh-head b{display:block;font-size:16px}
.rh-head .hint{display:block}
.rh-site{white-space:nowrap;font-size:14px}
.rh-tools{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.rh-tools input{flex:1;min-width:200px}
.rh-tools label{flex-direction:row;align-items:center;gap:8px;white-space:nowrap}
#vRh>.hint{margin:0 0 12px}
.rh-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:8px;align-items:start}
.rh-grid .group{grid-column:1/-1}
.rh-card{display:flex;gap:12px;padding:10px;background:var(--panel);border:1px solid transparent;border-radius:var(--r)}
.rh-card:hover,.rh-card:focus-within{border-color:var(--line-hi)}
.rh-cover{position:relative;flex:none;width:90px;height:120px;border-radius:4px;overflow:hidden;display:grid;place-items:center;
  background:var(--panel-hi);color:var(--dim);font-weight:700;font-size:20px}
.rh-cover img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.rh-tier{position:absolute;left:4px;top:4px;z-index:1;min-width:22px;height:22px;padding:0 5px;border-radius:4px;display:grid;place-items:center;
  font-size:13px;font-weight:700;color:#111;background:var(--tc)}
.rh-body{min-width:0;display:flex;flex-direction:column;gap:4px;flex:1}
.rh-title{font-weight:600;font-size:15px;line-height:1.3}
.rh-meta{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center;font-size:13px;color:var(--dim)}
.rh-stars{color:var(--text);font-weight:600;white-space:nowrap}
.rh-req{font-size:12.5px;color:var(--faint);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rh-act{display:flex;gap:10px;align-items:center;margin-top:auto;flex-wrap:wrap}
.rh-act button{min-height:36px;padding:6px 14px;font-size:13.5px}
.rh-act a{font-size:13.5px}
/* ---- Quiver: the same cards; player reports sit under the summary */
.qv-rep{font-size:12.5px;color:var(--faint)}
.qv-rep b{color:var(--text);font-weight:600}

/* ---- controller and D-pad: keep the focused control clear of the sticky header and the hint strip */
main :is(a,button,input,select,textarea,summary,[tabindex]){scroll-margin-top:84px;scroll-margin-bottom:24px}
.pad :focus{outline:3px solid var(--accent);outline-offset:2px}
.padhint{position:fixed;left:0;right:0;bottom:0;z-index:10;padding:6px 16px;background:var(--panel);border-top:1px solid var(--line);
  font-size:12.5px;color:var(--dim)}
body:has(.padhint:not([hidden])) main{padding-bottom:96px}

@media (max-width:760px){
  header{padding:10px 12px} main{padding:12px} .brand .sub:not(:has(#selfProg)){display:none}
  .app,.repo-row{grid-template-columns:minmax(0,1fr)} .actions{justify-content:flex-start}
  .rh-grid{grid-template-columns:1fr}
  .tabs{overflow-x:auto;scrollbar-width:none} .tabs>button{flex:none}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <symbol id="logo" viewBox="0 0 256 256"><g><path fill="#e6e8eb" fill-rule="evenodd" d="M60 120H196A44 44 0 0 1 240 164V176A44 44 0 0 1 196 220H60A44 44 0 0 1 16 176V164A44 44 0 0 1 60 120ZM84 140H172A6 6 0 0 1 178 146V194A6 6 0 0 1 172 200H84A6 6 0 0 1 78 194V146A6 6 0 0 1 84 140Z"/><path fill="#1a9fff" d="M163 122V96A3 3 0 0 1 163.88 93.88L192.16 65.59A17 17 0 0 0 168.12 41.55L139.84 69.84A37 37 0 0 0 129 96V122Z"/></g></symbol>
  <symbol id="gear" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></g></symbol>
  <symbol id="i-up" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 18V6M6.5 11.5 12 6l5.5 5.5"/></g></symbol>
  <symbol id="i-get" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/></g></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></symbol>
  <symbol id="i-sync" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 0 0-14.3-4.9M4 13a8 8 0 0 0 14.3 4.9"/><path d="M5 3v4h4M19 21v-4h-4"/></g></symbol>
  <symbol id="i-more" viewBox="0 0 24 24"><g fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></g></symbol>
</defs></svg>

<header>
  <div class="brand"><svg><use href="#logo"/></svg>
    <div><h1>SedotDeck<span class="ver-label">v__VERSION__</span></h1><div class="sub" id="sub">Keeps your AppImages, Flatpaks and PC ports up to date</div></div></div>
  <button id="btnCheck" class="icon ghost" title="Check for updates" aria-label="Check for updates"><svg><use href="#i-sync"/></svg></button>
  <button id="btnAdd"><svg><use href="#i-plus"/></svg> Add app</button>
  <button id="btnUpdAll" class="primary" hidden>Update all</button>
  <button id="btnSet" class="icon ghost" title="Settings" aria-label="Settings"><svg><use href="#gear"/></svg></button>
</header>
<main>
<nav class="tabs" id="tabs" role="tablist">
  <button type="button" role="tab" data-tab="apps" class="on">My apps</button>
  <button type="button" role="tab" data-tab="gh">Discover</button>
  <button type="button" role="tab" data-tab="rh"><img src="https://recomphaven.com/assets/icons/icon-192.png" alt="" onerror="this.remove()">Recomp Haven</button>
  <button type="button" role="tab" data-tab="qv">Quiver</button>
  <button type="button" role="tab" data-tab="rl">Recomp Launcher</button>
  <span class="tabs-end"><button type="button" class="ghost" id="btnSel">Select</button></span>
</nav>
<section id="vApps"><div id="found"></div><div class="summary" id="summary"></div><div id="list" class="list"></div></section>
<section id="vGh" hidden>
  <form class="search" id="ghForm"><input type="search" id="ghQ" placeholder="Search GitHub, e.g. god of war 3 or bloodborne" autocomplete="off" spellcheck="false" aria-label="Search GitHub">
    <select id="ghSort" aria-label="Sort results"><option value="best">Best match</option><option value="stars">Most stars</option><option value="updated">Recently updated</option></select>
    <button class="primary" type="submit">Search</button></form>
  <div class="hint" id="ghInfo">Finds GitHub projects by name or description. Add looks at the latest release before anything downloads.</div>
  <div id="ghList" class="list"></div>
</section>
<section id="vRh" hidden>
  <div class="rh-head"><img id="rhLogo" src="https://recomphaven.com/assets/icons/icon-192.png" alt="" onerror="this.remove()">
    <div><b>Recomp Haven</b><span class="hint">Recomps and PC ports with a Linux release. List, tiers and summaries by <a href="https://github.com/rikuwru/recomphaven" target="_blank" rel="noopener">Recomp Haven</a> (rikuwru). Box art belongs to each game's publisher, and every recomp needs your own copy of the game.</span></div>
    <a class="rh-site" href="https://recomphaven.com/" target="_blank" rel="noopener">recomphaven.com ↗</a></div>
  <div class="rh-tools"><input type="text" id="rhQ" placeholder="Search games or projects" autocomplete="off" spellcheck="false" aria-label="Search Recomp Haven">
    <label>System<select id="rhCon"><option value="">All systems</option></select></label>
    <label>Sort<select id="rhSort">
      <option value="tier">Tier</option><option value="system">System</option><option value="stars">Stars</option>
      <option value="name">Name</option><option value="released">Newest release</option></select></label></div>
  <div class="summary" id="rhChips"></div>
  <div class="hint" id="rhInfo"></div>
  <div id="rhList" class="rh-grid"></div>
</section>
<section id="vQv" hidden>
  <div class="rh-head">
    <div><b>Quiver catalog</b><span class="hint">Ports and recomps with a Linux release that Quiver has verified. List, descriptions and player reports from the catalog behind Quiver Launcher; SedotDeck isn't part of Quiver. Box art, mostly from SteamGridDB, belongs to each game's publisher, and every port needs your own copy of the game.</span></div>
    <a class="rh-site" href="https://quiverlauncher.com/" target="_blank" rel="noopener">quiverlauncher.com ↗</a></div>
  <div class="rh-tools"><input type="text" id="qvQ" placeholder="Search games or projects" autocomplete="off" spellcheck="false" aria-label="Search the Quiver catalog">
    <label>System<select id="qvCon"><option value="">All systems</option></select></label>
    <label>Sort<select id="qvSort">
      <option value="added">Newly added</option><option value="released">Newest release</option><option value="recommended">Most recommended</option><option value="stars">Most stars</option>
      <option value="system">System</option><option value="name">Name</option></select></label></div>
  <div class="summary" id="qvChips"></div>
  <div class="hint" id="qvInfo"></div>
  <div id="qvList" class="rh-grid"></div>
</section>
<section id="vRl" hidden>
  <div class="rh-head">
    <div><b>Recomp Launcher</b><span class="hint">PC recomps with a Linux release from the catalog behind RecompLauncher (recomplauncher.com); SedotDeck isn't part of it. Each entry links to its developer's GitHub. Box art belongs to each game's publisher, and every recomp needs your own copy of the game.</span></div>
    <a class="rh-site" href="https://recomplauncher.com/" target="_blank" rel="noopener">recomplauncher.com ↗</a></div>
  <div class="rh-tools"><input type="text" id="rlQ" placeholder="Search games or projects" autocomplete="off" spellcheck="false" aria-label="Search the Recomp Launcher catalog">
    <label>System<select id="rlCon"><option value="">All systems</option></select></label>
    <label>Sort<select id="rlSort">
      <option value="stars">Most stars</option><option value="released">Newest release</option><option value="added">Newly added</option>
      <option value="system">System</option><option value="name">Name</option></select></label></div>
  <div class="hint" id="rlInfo"></div>
  <div id="rlList" class="rh-grid"></div>
</section>
</main>
<div id="selbar" class="selbar" hidden>
  <span id="selN">0 selected</span>
  <button type="button" class="ghost" id="selAll">Select all</button>
  <span class="sel-acts"><button type="button" id="selCheck">Check</button><button type="button" id="selUpd">Update</button>
  <button type="button" id="selCat">Category…</button><button type="button" class="danger" id="selRm">Remove…</button></span>
  <button type="button" class="primary" id="selDone">Done</button>
</div>

<dialog id="dAdd"><form class="dlg" method="dialog" id="fAdd">
  <h2 id="addTitle">Add app</h2>
  <label id="aRepoLbl">Release link
    <input type="text" id="aRepo" placeholder="owner/repo, or https://github.com/owner/repo" autocomplete="off" spellcheck="false" required></label>
  <div class="hint" id="aInfo"></div>
  <div class="using" id="aUsing" hidden></div>
  <label class="row" id="aRenL" hidden><input type="checkbox" id="aRen" checked> <span>Rename to <span class="mono" id="aRenName"></span></span></label>
  <details class="adv" id="aAdv"><summary>Advanced: name, category, which release and file</summary><div>
    <label>Name<input type="text" id="aName" placeholder="Shown in the list, also the file name"></label>
    <label>Category<input type="text" id="aCatg" list="catList" maxlength="40" placeholder="Optional, e.g. Emulators. Apps with one are listed together"></label>
    <label>Skip releases named (regex)<input type="text" id="aSkip" placeholder="e.g. nightly|beta: releases whose title or tag match are ignored"></label>
    <label class="row"><input type="checkbox" id="aLatest"> <span>Only the release the developer marked “Latest”</span></label>
    <label>Asset pattern (regex)<input type="text" id="aPat" placeholder="auto: picks the Linux AppImage / Linux build"></label>
    <label class="row"><input type="checkbox" id="aPre"> Include pre-releases / nightlies</label>
    <div class="assets" id="aAssets" hidden></div>
    <div class="hint" id="aTip" hidden>Click a file to download that one instead. SedotDeck turns it into a pattern that keeps matching future versions.</div>
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
  <div class="inl" style="align-items:center"><span style="flex:1">SedotDeck __VERSION__ <span class="hint" id="sSelfInfo"></span></span><button type="button" id="sSelf">Check for SedotDeck updates</button></div>
  <label>Install folder<div class="inl"><input type="text" id="sDir"><button type="button" id="sOpen">Open folder</button></div></label>
  <label class="row"><input type="checkbox" id="sBak"> Keep previous version as <span class="mono">.old</span> (enables rollback)</label>
  <label>GitHub token (optional)<input type="password" id="sTok" placeholder="ghp_… (raises the rate limit from 60 to 5000/hour)"></label>
  <div class="hint" id="sTokInfo"></div>
  <details class="adv"><summary>More</summary><div>
    <label>App list</label>
    <div class="btns" style="justify-content:flex-start;margin-top:-6px">
      <button type="button" id="sExport">Export list</button>
      <button type="button" id="sImport">Import list</button>
      <input type="file" id="sImportFile" accept=".json,application/json" hidden>
    </div>
    <div class="hint" style="margin-top:-6px">Repos, names and patterns only. No token or paths.</div>
    <label>Storage</label>
    <div class="hint" id="sDisk" style="margin-top:-6px"></div>
    <div class="btns" style="justify-content:flex-start;margin-top:-4px">
      <button type="button" id="sClean">Delete .old backups</button>
      <button type="button" id="sUnhide">Show hidden files again</button>
    </div>
  </div></details>
  <div class="btns" style="justify-content:space-between">
    <span><button type="button" class="danger ghost" id="sQuit">Quit SedotDeck</button></span>
    <span><button type="button" class="ghost" onclick="dSet.close()">Cancel</button> <button class="primary" type="submit">Save</button></span>
  </div>
</form></dialog>

<dialog id="dLaunch"><form class="dlg" method="dialog">
  <h2 id="lTitle">Choose program</h2>
  <p>What Launch and the Steam shortcut start. SedotDeck keeps your choice across updates.</p>
  <div class="assets progs" id="lList"></div>
  <div class="hint" id="lHint"></div>
  <div class="btns"><button type="button" class="ghost" onclick="dLaunch.close()">Cancel</button><button type="button" class="primary" id="lOk">Use this</button></div>
</form></dialog>

<dialog id="dCat"><form class="dlg" method="dialog" id="fCat">
  <h2 id="cTitle">Category</h2>
  <p>Apps with the same category are listed together.</p>
  <label>Category<input type="text" id="cName" list="catList" maxlength="40" placeholder="e.g. Emulators, Recomps" autocomplete="off"></label>
  <datalist id="catList"></datalist>
  <div class="btns"><button type="button" class="ghost" onclick="dCat.close()">Cancel</button><button type="button" class="ghost" id="cClear">No category</button><button class="primary" type="submit">Save</button></div>
</form></dialog>

<dialog id="dAsk"><form class="dlg" method="dialog">
  <h2 id="askTitle"></h2><p id="askText"></p>
  <label class="row" id="askCheckRow"><input type="checkbox" id="askCheck"> <span id="askCheckText"></span></label>
  <div class="btns"><button type="button" class="ghost" id="askNo">Cancel</button><button type="button" class="primary" id="askYes">OK</button></div>
</form></dialog>
<dialog id="dSelf"><form class="dlg" method="dialog">
  <h2 id="suTitle"></h2>
  <div class="hint" id="suMeta"></div>
  <div class="notes" id="suBody"></div>
  <div class="btns"><button type="button" class="ghost" id="suSkip">Skip this version</button><button type="button" class="ghost" id="suLater">Later</button><button type="button" class="primary" id="suGo">Update now</button><button type="button" class="ghost" id="suClose">Close</button><button type="button" class="primary" id="suPrimary">Keep using SedotDeck</button></div>
</form></dialog>
<div id="menu" class="menu" role="menu" hidden></div>
<div id="toast"></div>
<div id="padHint" class="padhint" aria-hidden="true" hidden>A Select · B Back · X App menu · Y Search · LB / RB Tabs · Start Settings</div>

<script>
const TOKEN="__TOKEN__";
// Opened by the desktop app (Tauri webview) rather than a browser tab.
const APP=new URLSearchParams(location.search).has("app");
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S={apps:[],jobs:{},settings:{}}, editId=null, timer=null;

async function api(path,body={}){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-SedotDeck-Token":TOKEN},body:JSON.stringify(body)});
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
const doneAt={}; let prevJobs={}, FILTER="all";
try{FILTER=localStorage.getItem("sedotdeck.filter")||"all"}catch(e){}

// One status per app; it drives the status text, sort order and filter.
function status(a){
  const job=S.jobs[a.id], inst=a.installed&&a.exists;
  if(job&&job.state=="running") return "busy";
  if(a.error) return "err";
  if(!inst) return "new";
  if(a.has_update) return "update";
  return a.latest&&a.latest.tag?"ok":"busy";
}
const ORDER={update:0,busy:1,err:2,new:3,ok:4};
const STATE={update:"Update available",ok:"Up to date",new:"Not installed",err:"Error",busy:"Checking…"};

// Nightly builds are tagged with long hashes ("build-4024ea31e049f3…"): keep rows to one line.
const short=t=>{t=String(t||"");return t.length>16?t.slice(0,14)+"…":t};
function card(a){
  const job=S.jobs[a.id], inst=a.installed, here=!!(inst&&a.exists), L=a.latest||{}, st=status(a);
  const running=job&&job.state=="running";
  let jobHtml="";
  if(job){
    if(running){
      const dl=job.label=="Downloading"&&job.progress;
      jobHtml=`<div class="bar ${dl?"dl":"wait"}"><i style="transform:scaleX(${dl?job.progress.toFixed(3):1})"></i></div><div class="jobtxt">${esc(job.label)}… ${esc(job.message)}</div>`;
    }
    else if(job.state=="error") jobHtml=`<div class="err">${esc(job.message)}</div>`;
  }
  if(doneAt[a.id]&&Date.now()-doneAt[a.id]<4000&&!running)
    jobHtml=`<div class="done-check">Installed ${esc(inst&&inst.tag||"")}</div>`;
  const mainBtn=st=="err"?"Edit":!here?"Install":(a.has_update?"Update":null);
  // One line: the version you have and, if there is one, the version you'd get.
  const cur=here?(inst.tag=="unknown"?"your copy":inst.tag):"";
  let ver="";
  if(st=="update")ver=`<span class="mono" title="${esc(L.tag)}">${esc(short(cur))} → <b>${esc(short(L.tag))}</b></span>`;
  else if(here)ver=`<span class="mono" title="${esc(inst.tag)}">${esc(short(cur))}</span>`+(a.skipped?`<span>${esc(short(L.tag))} skipped</span><a href="#" data-a="unskip" data-id="${a.id}">undo</a>`:"");
  else if(L.tag)ver=`<span class="mono" title="${esc(L.tag)}">${esc(short(L.tag))}</span>`+(L.asset&&L.asset.size?`<span>${mb(L.asset.size)}</span>`:"");
  if(ver&&L.prerelease)ver+='<span class="tag">pre-release</span>';
  if(here&&inst.kind=="flatpak")ver+='<span class="tag">Flatpak</span>';
  if(st=="update"&&L.notes)ver+=`<a href="#" data-a="notes" data-id="${a.id}">What's new</a>`;
  if(here&&inst.verified)ver+=`<span class="tag ok" title="SHA-256 ${esc(inst.sha256)} (${esc(inst.verified_by)})">verified</span>`;
  if(a.in_steam)ver+='<span class="tag ok">in Steam</span>';
  const notes=[];
  // A file the user brought keeps its own (often versioned) name, so show which one it is.
  if(a.target_path&&a.file_name)notes.push(`<span class="mono" title="Your file, updated in place">${esc(a.file_name)}</span>`);
  if(a.error&&!job)notes.push(`<span class="err">${esc(a.error)}</span>`);
  if(a.notice&&!a.error&&!job)notes.push(esc(a.notice));
  if(a.moved_to&&!job)notes.push(`Repo moved to <span class="mono">${esc(a.moved_to)}</span> · <a href="#" data-a="move" data-id="${a.id}">Use new link</a>`);
  const opens=inst&&inst.kind=="archive"&&!a.has_launcher;
  return `<div class="app st-${st} ${SEL&&SEL.has(a.id)?"sel":""}" data-card="${a.id}"><span class="pickbox"></span>
   <div><div class="name">${esc(a.name)}</div>
     <a class="repo" href="${esc(a.web_url)}" target="_blank" rel="noopener">${esc(a.display_repo)}</a>
     ${notes.map(n=>`<div class="note">${n}</div>`).join("")}</div>
   <div><div class="state">${STATE[st]}</div>${ver?`<div class="ver">${ver}</div>`:""}</div>
   <div class="actions">
     ${mainBtn=="Edit"?`<button class="primary" data-a="edit" data-id="${a.id}">Edit</button>`:mainBtn?`<button class="primary" data-a="update" data-id="${a.id}" ${running||!L.asset?"disabled":""}>${ICON(mainBtn=="Update"?"i-up":"i-get")} ${mainBtn}</button>`:""}
     ${here?`<button data-a="${opens?"folder":"launch"}" data-id="${a.id}" ${running?"disabled":""}>${opens?"Open":"Launch"}</button>`:""}
     <button class="ghost icon more" data-a="menu" data-id="${a.id}" title="More" aria-label="More actions for ${esc(a.name)}" aria-haspopup="menu">${ICON("i-more")}</button>
   </div>
   ${jobHtml?`<div class="job">${jobHtml}</div>`:""}</div>`;
}

function render(){
  renderFound();
  // Remember installs that just finished, for the check-mark animation.
  for(const [id,p] of Object.entries(prevJobs)){
    const j=S.jobs[id];
    if(id=="__self__"){if(p.state=="running"&&j&&j.state!="running")selfEnd(j);continue}
    if(p.state=="running"&&p.label!="Checking"&&j&&j.state=="done") doneAt[id]=Date.now();
  }
  prevJobs=S.jobs||{};
  const L=$("#list"), apps=[...S.apps].sort((x,y)=>ORDER[status(x)]-ORDER[status(y)]||x.name.localeCompare(y.name));
  const count=k=>apps.filter(a=>status(a)==k).length;
  const chips=[["all","All",apps.length],["update","Updates",count("update")],["ok","Up to date",count("ok")],["new","Not installed",count("new")]];
  if(count("err")) chips.push(["err","Errors",count("err")]);
  if(!chips.some(c=>c[0]==FILTER)) FILTER="all";
  // Filters only help with a long list; a short one is already sorted with updates first.
  const useChips=apps.length>=8;
  if(!useChips)FILTER="all";
  $("#summary").innerHTML=useChips?chips.map(([k,t,n])=>`<button class="${FILTER==k?"on":""}" data-f="${k}" aria-pressed="${FILTER==k}">${t}<b>${n}</b></button>`).join(""):"";
  const shown=FILTER=="all"?apps:apps.filter(a=>status(a)==FILTER);
  if(!S.apps.length&&!foundLater&&(S.found||[]).some(f=>f.src)){L.innerHTML=""}  // the found banner is the next step
  else if(!S.apps.length){L.innerHTML='<div class="empty"><b>No apps yet</b>Use Add app with a link to any release page, <a href="#" data-tab="gh">search GitHub</a> for a game, or browse <a href="#" data-tab="rh">Recomp Haven</a>, the <a href="#" data-tab="qv">Quiver catalog</a> and <a href="#" data-tab="rl">Recomp Launcher</a>.</div>'}
  else if(!shown.length){L.innerHTML='<div class="empty"><b>Nothing here</b>No apps match this filter.</div>'}
  else{
    const cats=[...new Set(shown.map(a=>a.category||""))].sort((x,y)=>!x-!y||x.localeCompare(y));
    L.innerHTML=cats.some(c=>c)?cats.map(c=>{const g=shown.filter(a=>(a.category||"")==c);
      return `<h3 class="group">${c?esc(c):"Other"}<span>${g.length}</span></h3>`+g.map(card).join("")}).join(""):shown.map(card).join("");
  }
  const n=count("update");
  const b=$("#btnUpdAll");b.hidden=!n;b.textContent=`Update all (${n})`;
  const checking=Object.values(S.jobs||{}).some(j=>j.state=="running"&&j.label=="Checking");
  const last=Math.max(0,...S.apps.map(a=>a.checked_at||0));
  $("#btnCheck svg").classList.toggle("spin",checking);
  $("#btnSel").hidden=S.apps.length<2;
  // The Recomp Haven tab marks what's in My apps; redraw it only when that changes.
  if(TAB=="rh"&&RH.items&&S.apps.map(a=>a.host+"/"+a.repo).join()!=rhKey)renderRh();
  if(TAB=="qv"&&QV.items&&S.apps.map(a=>a.host+"/"+a.repo).join()!=qvKey)renderQv();
  if(TAB=="rl"&&RL.items&&S.apps.map(a=>a.host+"/"+a.repo).join()!=rlKey)renderRl();
  if(TAB=="gh"&&GH.items&&S.apps.map(a=>a.host+"/"+a.repo).join()!=ghKey)renderGh();
  if(SEL){for(const id of [...SEL])if(!S.apps.some(a=>a.id==id))SEL.delete(id);renderSel()}
  $("#catList").innerHTML=[...new Set(S.apps.map(a=>a.category).filter(Boolean))].sort().map(c=>`<option value="${esc(c)}">`).join("");
  const self=S.jobs.__self__, selfRun=self&&self.state=="running";
  if(selfRun){selfSample(self.done||0);if(dSelf.open&&selfMode=="progress")selfPaint()}
  // While SedotDeck downloads its own update, the subtitle is a button that opens the progress view.
  if(selfRun){
    if(!$("#selfProg"))$("#sub").innerHTML='<button type="button" class="ghost" id="selfProg"><span></span><span class="bar"><i></i></span></button>';
    $("#selfProg>span").textContent=`Updating SedotDeck… ${Math.round((self.progress||0)*100)}%`;
    $("#selfProg i").style.transform=`scaleX(${(self.progress||0).toFixed(3)})`;
  }
  else $("#sub").textContent=selfMode=="done"?`SedotDeck ${selfTag} installed. Restart to use it.`
    :checking?"Checking for updates…":S.apps.length?`Checked ${ago(last)}`:"Keeps your AppImages, Flatpaks and PC ports up to date";
}
$("#sub").addEventListener("click",e=>{if(e.target.closest("#selfProg"))openSelf()});
$("#summary").addEventListener("click",e=>{
  const c=e.target.closest("button[data-f]");if(!c)return;
  FILTER=c.dataset.f;try{localStorage.setItem("sedotdeck.filter",FILTER)}catch(err){}
  render();
});
let autoChecked=false,selfChecked=false;
async function refresh(){
  try{
    S=await api("/api/state");render();
    if(!autoChecked){
      // Opening SedotDeck checks for updates by itself, at most once an hour.
      autoChecked=true;const last=Math.max(0,...S.apps.map(a=>a.checked_at||0));
      if(S.apps.length&&Date.now()/1000-last>3600)act("/api/check",{});
    }
    if(!selfChecked){selfChecked=true;checkSelf(false)}
  }catch(e){$("#list").innerHTML='<div class="empty"><b>SedotDeck is not running</b>Start it again from the app menu.</div>'}
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
    const r=await ask({title:`Remove ${a.name}?`,text:"SedotDeck will stop tracking it.",ok:"Remove",danger:true,
                       check:a.installed&&a.exists?"Also delete its downloaded files":""});
    if(r)act("/api/remove",{id,delete_files:!!r.checked},"Removed");
  }
  else if(k=="edit")openAdd(a);
  else if(k=="notes")openNotes(a);
  else if(k=="release"){const u=(a.latest&&a.latest.url)||a.web_url;APP?act("/api/open_url",{url:u}):window.open(u,"_blank","noopener")}
  else if(k=="skip")act("/api/skip",{id},`Skipped ${a.latest.tag}. SedotDeck will tell you about the next version.`);
  else if(k=="unskip")act("/api/skip",{id,undo:true},`${a.name}: update offered again`);
  else if(k=="folder")act("/api/open_folder",{id});
  else if(k=="rename")act("/api/rename",{id},r=>`Renamed to ${r.file}`);
  else if(k=="steam"&&!a.steam_exe)openLaunch(a,true);
  else if(k=="steam")addSteam(a);
  else if(k=="program")openLaunch(a,false);
  else if(k=="move")act("/api/move",{id},r=>`${a.name} now follows ${r.repo}`);
  else if(k=="category")openCat([a.id]);
  else if(k=="rhpage")openUrl(RH.site+"#"+encodeURIComponent(a.rh_id));
  else if(k=="qvpage")openUrl(QV.site+"apps/"+encodeURIComponent(a.qv_id));
  else if(k=="rlpage")openUrl(RL.site+"game/"+encodeURIComponent(a.rl_id));
}
function openUrl(u){APP?act("/api/open_url",{url:u}):window.open(u,"_blank","noopener")}
function addSteam(a){
  act("/api/steam_add",{id:a.id},r=>!r.added?`${a.name} is already in Steam`:r.proton
    ?`${a.name} added to Steam. Start Steam, then Properties > Compatibility > force Proton.`
    :`${a.name} added to Steam. Start Steam to see it.`);
}

// Pick which program inside a folder app Launch and Steam start: automatic, or one the user chose.
async function openLaunch(a,thenSteam){
  let r;try{r=await api("/api/launch_choices",{id:a.id})}catch(e){toast(e.message,true);return}
  const KIND={linux:'<span class="tag">Linux</span>',windows:'<span class="tag">Windows · Proton</span>'};
  const rows=[`<div data-f="" class="${r.manual?"":"pick"}"><b>Automatic</b><small>${r.manual||!r.current?"SedotDeck picks":esc(r.current)}</small></div>`]
    .concat(r.choices.map(c=>`<div data-f="${esc(c.file)}" class="${r.manual&&c.file==r.current?"pick":""}"><span class="mono">${esc(c.file)}</span>${KIND[c.kind]||""}<small>${mb(c.size)}</small></div>`));
  const L=$("#lList");L.innerHTML=rows.join("");
  $("#lTitle").textContent=`Choose program · ${a.name}`;
  $("#lHint").textContent=r.choices.length?"Windows .exe files run through Proton, from Steam only.":"No program found in this folder. Open the folder to check what the release contains.";
  L.onclick=e=>{const d=e.target.closest("[data-f]");if(!d)return;L.querySelectorAll(".pick").forEach(x=>x.classList.remove("pick"));d.classList.add("pick")};
  const ok=$("#lOk");ok.textContent=thenSteam?"Use and add to Steam":"Use this";
  ok.onclick=async()=>{
    const p=L.querySelector(".pick"),file=p?p.dataset.f:"";
    dLaunch.close();
    try{
      const s=await api("/api/set_launch",{id:a.id,file});
      if(!s.launch){toast("No program found, so nothing to start",true);refresh();return}
      if(thenSteam)addSteam(a);else{toast(`${a.name} starts ${s.launch.file}`);refresh()}
    }catch(e){toast(e.message,true)}
  };
  dLaunch.showModal();
}
$("#list").addEventListener("click",e=>{
  if(SEL){
    const c=e.target.closest("[data-card]");if(!c)return;
    e.preventDefault();const id=c.dataset.card;SEL.has(id)?SEL.delete(id):SEL.add(id);
    c.classList.toggle("sel",SEL.has(id));renderSel();return;
  }
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
  if(S.steam&&here&&!a.in_steam)
    rows.push(it("steam","Add to Steam",{off:S.steam_running,hint:S.steam_running?"Close Steam first (Steam menu > Exit)":a.steam_exe?"Adds it as a Non-Steam game":"Pick its program first"}));
  if(here&&a.installed.kind=="archive"){
    const lc=a.launch||{};
    rows.push(it("program","Choose program…",{off:running,hint:lc.file?esc(lc.file)+(lc.manual?"":" (automatic)"):a.has_launcher?"Automatic":"Not chosen yet"}));
    rows.push(it("folder","Open folder"));
  }
  if(a.rename_to){const lock=a.in_steam&&S.steam_running;
    rows.push(it("rename",`Rename file to ${esc(a.rename_to)}`,{off:lock,hint:lock?"Close Steam first (Steam menu > Exit)":a.in_steam?"Also updates its Steam shortcut":""}))}
  rows.push(it("release","Open release page"));
  if(a.rh_id)rows.push(it("rhpage","View on Recomp Haven"));
  if(a.qv_id)rows.push(it("qvpage","View on quiverlauncher.com"));
  if(a.rl_id)rows.push(it("rlpage","View on recomplauncher.com"));
  rows.push("<hr>",it("category","Category…",{hint:a.category?esc(a.category):""}),it("edit","Edit"),it("remove","Remove…",{danger:true}));
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

// Banner: AppImages already in the install folder that SedotDeck could keep up to date.
let foundKey="",foundLater=false,foundOpen=false;
function renderFound(){
  const F=S.found||[],box=$("#found");
  if(foundLater||!F.length){box.innerHTML="";foundKey="";return}
  const key=JSON.stringify([foundOpen,F.map(f=>[f.path,f.app_id||"",f.src])]);
  if(key==foundKey)return;  // keep the user's checkbox choices between polls
  foundKey=key;
  const known=F.filter(f=>f.src),unknown=F.filter(f=>!f.src);
  if(!foundOpen){
    // One quiet line; Review opens the list.
    box.innerHTML=`<div class="found-line"><span>${F.length} AppImage${F.length>1?"s":""} found in ${esc(S.settings.install_dir_short||"")}</span>
      <button type="button" class="ghost" id="fOpen">Review</button><button type="button" class="ghost" id="fHideAll">Hide</button></div>`;
    return;
  }
  const row=f=>f.src
    ?`<label class="frow"><input type="checkbox" data-p="${esc(f.path)}" checked><span><b>${esc(f.name)}</b> <span class="dim">${f.app_id?"for the "+esc(f.name)+" entry you added":esc(f.display)}</span><small class="mono">${esc(f.file)} · ${mb(f.size)}</small></span></label>`
    :`<div class="frow"><span><span class="mono">${esc(f.file)}</span><small>Not recognized · ${mb(f.size)}</small></span><button type="button" data-link="${esc(f.path)}">Choose repo…</button><button type="button" class="ghost" data-ign="${esc(f.path)}">Hide</button></div>`;
  box.innerHTML=`<div class="found"><h3>${F.length} AppImage${F.length>1?"s":""} found in ${esc(S.settings.install_dir_short||"")}</h3>
    <div class="rows">${known.map(row).join("")}${unknown.map(row).join("")}</div>
    ${known.length?`<label class="frow"><input type="checkbox" id="fRen" checked><span>Rename to clean names <span class="dim">(Name.AppImage)</span></span></label>`:""}
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
  else if(t.id=="fHideAll"){if((S.found||[]).some(f=>f.src)){foundLater=true;renderFound();render();return}foundKey="";act("/api/scan_ignore",{paths:(S.found||[]).filter(f=>!f.src).map(f=>f.path)},"Hidden. Settings can show them again.")}
  else if(t.id=="fTrack"){
    const items=[...$("#found").querySelectorAll("input[data-p]:checked")].map(i=>byPath(i.dataset.p)).filter(Boolean)
      .map(f=>({path:f.path,src:f.src,name:f.name,app_id:f.app_id||""}));
    t.disabled=true;foundKey="";
    act("/api/track_found",{items,rename:!!($("#fRen")&&$("#fRen").checked)},r=>`Tracking ${r.count} app(s), checking for updates…`);
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

let ADOPT=null,prevTimer=null,lastPrev="";
const isLink=v=>/[\/.]/.test(v.trim());  // owner/repo or a URL
function openAdd(a,adopt){
  editId=a?a.id:null;ADOPT=adopt||null;
  $("#addTitle").textContent=a?"Edit "+a.name:"Add app";
  $("#aSave").textContent=a?"Save":"Add";
  $("#aRepoLbl").firstChild.textContent=a?"Repository":"Release link";
  // Non-GitHub repos need their host, or the preview would look them up on GitHub.
  $("#aRepo").value=a?(a.host&&a.host!="github.com"?`https://${a.host}/${a.repo}`:a.repo):"";$("#aRepo").disabled=!!a;
  $("#aName").value=a?a.name:"";$("#aPat").value=a?a.pattern||"":"";$("#aPre").checked=a?!!a.prerelease:false;
  $("#aCatg").value=a?a.category||"":"";$("#aSkip").value=a?a.skip_titles||"":"";$("#aLatest").checked=a?!!a.latest_only:false;
  $("#aAssets").hidden=true;$("#aTip").hidden=true;$("#aInfo").textContent="";lastPrev="";
  $("#aAdv").open=!!a;
  $("#aUsing").hidden=$("#aRenL").hidden=!ADOPT;$("#aRen").checked=true;renName();
  if(ADOPT)$("#aUsing").innerHTML=`SedotDeck will keep <span class="mono">${esc(ADOPT.file)}</span> up to date. It keeps its name, so a Steam shortcut to it keeps working.`;
  dAdd.showModal();
  if(a)preview();else $("#aRepo").focus();
}
function renName(){$("#aRenName").textContent=($("#aName").value.trim().replace(/[^\w.-]+/g,"-").replace(/^-+|-+$/g,"")||"Name")+".AppImage"}
$("#aName").addEventListener("input",renName);
$("#btnAdd").onclick=()=>openAdd(null);
const reEsc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
function patternFor(name){
  // Replace version-like chunks with wildcards so the pattern keeps matching future releases.
  return "^"+reEsc(name).replace(/v?\d+(\\\.\d+)+[\w-]*?(?=[-_\\]|$)/g,".*").replace(/(\.\*)+/g,".*")+"$";
}
function previewSoon(){clearTimeout(prevTimer);prevTimer=setTimeout(preview,600)}
$("#aRepo").oninput=()=>{
  if(isLink($("#aRepo").value))previewSoon();
  else{clearTimeout(prevTimer);lastPrev="";$("#aInfo").textContent="";$("#aAssets").hidden=true;$("#aTip").hidden=true}
};
$("#aPat").oninput=$("#aSkip").oninput=previewSoon;$("#aPre").onchange=$("#aLatest").onchange=preview;
async function preview(){
  const repo=$("#aRepo").value.trim();if(!isLink(repo))return;
  const me=lastPrev=JSON.stringify([repo,$("#aPat").value,$("#aPre").checked,$("#aSkip").value,$("#aLatest").checked]),box=$("#aAssets");
  $("#aInfo").textContent="Looking up the latest release…";
  try{
    const r=await api("/api/preview",{repo,pattern:$("#aPat").value,prerelease:$("#aPre").checked,skip_titles:$("#aSkip").value,latest_only:$("#aLatest").checked});
    if(me!=lastPrev)return;  // a newer lookup is on its way
    $("#aInfo").innerHTML=`<b>${esc(r.repo)}</b> · ${esc(r.tag)} · `+(r.picked?`will download <span class="mono">${esc(r.picked)}</span>`:"no Linux build found. Open Advanced and pick the right file.");
    box.innerHTML=r.assets.map(x=>`<div class="${x.name==r.picked?"pick":""}" data-n="${esc(x.name)}">${esc(x.name)}<small>${mb(x.size)}</small></div>`).join("")||"<div>No files in this release</div>";
    box.hidden=false;$("#aTip").hidden=false;
    if(!r.picked)$("#aAdv").open=true;
  }catch(e){if(me!=lastPrev)return;$("#aInfo").textContent=e.message;box.hidden=true;$("#aTip").hidden=true}
}
$("#aAssets").onclick=e=>{const d=e.target.closest("[data-n]");if(!d)return;$("#aPat").value=patternFor(d.dataset.n);preview()};
$("#fAdd").onsubmit=async e=>{
  e.preventDefault();
  if(!editId&&!isLink($("#aRepo").value)){
    toast("Paste a release link: owner/repo or the repo's URL",true);
    return;
  }
  const body={name:$("#aName").value,pattern:$("#aPat").value,prerelease:$("#aPre").checked,
    category:$("#aCatg").value,skip_titles:$("#aSkip").value,latest_only:$("#aLatest").checked,adopt_path:ADOPT?ADOPT.path:"",rename:!!ADOPT&&$("#aRen").checked};
  try{
    if(editId)await api("/api/edit",{id:editId,...body});
    else await api("/api/add",{repo:$("#aRepo").value,...body});
    dAdd.close();foundKey="";toast(ADOPT?"Added, using your existing file":editId?"Saved":"Added, checking…");refresh();
  }catch(err){toast(err.message,true)}
};

// ---- categories and select mode
let SEL=null;  // ids picked while selecting, or null
function setSel(on){SEL=on?new Set():null;document.body.classList.toggle("selecting",on);$("#selbar").hidden=!on;closeMenu();render();renderSel()}
function renderSel(){
  if(!SEL)return;const n=SEL.size;$("#selN").textContent=`${n} selected`;
  $("#selbar").querySelectorAll(".sel-acts button").forEach(b=>b.disabled=!n);
  $("#selAll").textContent=n&&n==S.apps.length?"Select none":"Select all";
}
$("#btnSel").onclick=()=>setSel(!SEL);
$("#selDone").onclick=()=>setSel(false);
$("#selAll").onclick=()=>{SEL=SEL.size==S.apps.length?new Set():new Set(S.apps.map(a=>a.id));render();renderSel()};
$("#selCheck").onclick=()=>act("/api/check",{ids:[...SEL]},`Checking ${SEL.size} app(s)…`);
$("#selUpd").onclick=()=>act("/api/update",{ids:[...SEL]},r=>r.count?`Updating ${r.count} app(s)`:"The selected apps are up to date");
$("#selCat").onclick=()=>openCat([...SEL]);
$("#selRm").onclick=async()=>{
  const ids=[...SEL],r=await ask({title:`Remove ${ids.length} app(s)?`,text:"SedotDeck will stop tracking them.",ok:"Remove",danger:true,check:"Also delete their downloaded files"});
  if(r)act("/api/remove",{ids,delete_files:!!r.checked},x=>`Removed ${x.count} app(s)`);
};
let catIds=[];
function openCat(ids){
  catIds=ids;const apps=S.apps.filter(a=>ids.includes(a.id)),same=[...new Set(apps.map(a=>a.category||""))];
  $("#cTitle").textContent=apps.length==1?`Category · ${apps[0].name}`:`Category · ${apps.length} apps`;
  $("#cName").value=same.length==1?same[0]:"";dCat.showModal();$("#cName").focus();
}
function saveCat(c){dCat.close();act("/api/category",{ids:catIds,category:c},c?`Moved to ${c}`:"Category removed")}
$("#fCat").onsubmit=e=>{e.preventDefault();saveCat($("#cName").value.trim())};
$("#cClear").onclick=()=>saveCat("");

// ---- tabs
let TAB="apps";
function setTab(t){
  TAB=t;try{localStorage.setItem("sedotdeck.tab",t)}catch(e){}
  $("#tabs").querySelectorAll("[data-tab]").forEach(b=>{b.classList.toggle("on",b.dataset.tab==t);b.setAttribute("aria-selected",b.dataset.tab==t)});
  $("#vApps").hidden=t!="apps";$("#vGh").hidden=t!="gh";$("#vRh").hidden=t!="rh";$("#vQv").hidden=t!="qv";$("#vRl").hidden=t!="rl";$(".tabs-end").hidden=t!="apps";
  if(t!="apps"&&SEL)setSel(false);
  if(t=="rh")loadRh(false);
  if(t=="qv")loadQv(false);
  if(t=="rl")loadRl(false);
  if(t=="gh")$("#ghQ").focus();
}
document.addEventListener("click",e=>{const b=e.target.closest("[data-tab]");if(b){e.preventDefault();setTab(b.dataset.tab)}});

const stars=n=>n>=1e3?(n/1e3).toFixed(n>=1e4?0:1).replace(/\.0$/,"")+"k":String(n);

// ---- Recomp Haven
const TIERS={S:["Definitive","#f5c542"],A:["Complete","#8bd17c"],B:["Playable","#6fb8ff"],C:["Rough","#f0a35e"],D:["Experimental","#a3a9b8"]};
const STAGES={released:"Released",beta:"Beta",development:"In development",abandoned:"Abandoned"};
let RH={items:null,site:"https://recomphaven.com/",consoles:{}},rhTier="all",rhDeck=false,rhLoading=false,rhAdding=new Set(),rhKey="";
try{$("#rhSort").value=localStorage.getItem("sedotdeck.rhsort")||"tier"}catch(e){}
async function loadRh(refresh){
  if(rhLoading||(RH.items&&!refresh))return renderRh();
  rhLoading=true;$("#rhInfo").textContent="Loading the list from Recomp Haven…";
  try{RH=await api("/api/recomphaven",{refresh})}catch(e){$("#rhInfo").textContent=e.message}
  rhLoading=false;renderRh();
}
const rhCon=k=>RH.consoles[k]||{label:k||"Other",short:(k||"?").toUpperCase(),color:""};
const tierRank=x=>x.tier?"SABCD".indexOf(x.tier):9;
const RH_SORT={
  tier:(a,b)=>tierRank(a)-tierRank(b)||b.stars-a.stars,
  system:(a,b)=>rhCon(a.console).label.localeCompare(rhCon(b.console).label)||tierRank(a)-tierRank(b)||b.stars-a.stars,
  stars:(a,b)=>b.stars-a.stars,
  name:(a,b)=>a.title.localeCompare(b.title),
  released:(a,b)=>String(b.released).localeCompare(String(a.released)),
};
function rhCard(x,tracked){
  const c=rhCon(x.console),t=TIERS[x.tier],added=tracked.has(x.key),busy=rhAdding.has(x.id);
  const initials=x.title.replace(/^the\s+/i,"").split(/\s+/).slice(0,2).map(w=>w[0]).join("").toUpperCase();
  return `<div class="rh-card" title="${esc(x.summary)}">
    <div class="rh-cover">${esc(initials)}${x.cover?`<img src="${esc(x.cover)}" alt="" loading="lazy" onerror="this.remove()">`:""}
      ${t?`<span class="rh-tier" style="--tc:${t[1]}" title="Tier ${x.tier}: ${t[0]}">${x.tier}</span>`:""}</div>
    <div class="rh-body">
      <div class="rh-title">${esc(x.title)}</div>
      <div class="rh-meta"><span class="tag" title="${esc(c.label)}">${esc(c.short)}</span>${esc(x.project)} · <span class="mono">${esc(short(x.version))}</span> · ${esc(STAGES[x.stage]||x.stage)}${x.deck?' <span class="tag ok">Steam Deck</span>':""}${x.stars>0?`<span class="rh-stars" title="${x.stars.toLocaleString()} GitHub stars">★ ${stars(x.stars)}</span>`:""}</div>
      ${x.requires?`<div class="rh-req">${esc(x.requires)}</div>`:""}
      <div class="rh-act">${added?'<span class="added">In My apps</span>':`<button class="primary" data-rh="${esc(x.id)}" ${busy?"disabled":""}>${busy?"Adding…":"Add"}</button>`}
        <a href="${esc(RH.site)}#${encodeURIComponent(x.id)}" target="_blank" rel="noopener">Details ↗</a></div>
    </div></div>`;
}
function renderRh(){
  if(!RH.items)return;
  if(RH.logo&&$("#rhLogo"))$("#rhLogo").src=RH.logo;
  rhKey=S.apps.map(a=>a.host+"/"+a.repo).join();
  const tracked=new Set(S.apps.map(a=>(a.host+"/"+a.repo).toLowerCase()));
  // The system list only offers systems that have something to install, with counts.
  const sel=$("#rhCon"),cur=sel.value,per={};
  RH.items.forEach(x=>per[x.console]=(per[x.console]||0)+1);
  sel.innerHTML='<option value="">All systems</option>'+Object.keys(per).sort((a,b)=>rhCon(a).label.localeCompare(rhCon(b).label))
    .map(k=>`<option value="${esc(k)}">${esc(rhCon(k).label)} (${per[k]})</option>`).join("");
  sel.value=per[cur]?cur:"";
  const q=$("#rhQ").value.trim().toLowerCase(),con=sel.value,sort=$("#rhSort").value;
  const list=RH.items.filter(x=>(rhTier=="all"||x.tier==rhTier||(rhTier=="CD"&&"CD".includes(x.tier)))&&(!rhDeck||x.deck)&&(!con||x.console==con)
    &&(!q||[x.title,x.project,x.genre,rhCon(x.console).label,rhCon(x.console).short].join(" ").toLowerCase().includes(q))).sort(RH_SORT[sort]||RH_SORT.tier);
  const n=t=>RH.items.filter(x=>t=="CD"?"CD".includes(x.tier):x.tier==t).length;
  $("#rhChips").innerHTML=[["all","All tiers",RH.items.length],["S","S · Definitive",n("S")],["A","A · Complete",n("A")],["B","B · Playable",n("B")],["CD","C–D · Early",n("CD")]]
    .map(([k,t,c])=>`<button class="${rhTier==k?"on":""}" data-tier="${k}" aria-pressed="${rhTier==k}">${t}<b>${c}</b></button>`).join("")
    +`<button class="${rhDeck?"on":""}" data-deck="1" aria-pressed="${rhDeck}">Steam Deck</button>`;
  const when=RH.fetched_at?`Updated ${ago(RH.fetched_at)}`:"";
  $("#rhInfo").innerHTML=`${list.length} of ${RH.items.length} recomps with a Linux release · ${when} · <a href="#" id="rhRefresh">Refresh</a>`+(RH.stale?` · ${esc(RH.stale)}`:"");
  if(!list.length){$("#rhList").innerHTML='<div class="empty" style="grid-column:1/-1"><b>Nothing here</b>No recomps match these filters.</div>';return}
  if(sort=="system"&&!con){
    // Grouped under a heading per system.
    const groups=[...new Set(list.map(x=>x.console))];
    $("#rhList").innerHTML=groups.map(k=>{const g=list.filter(x=>x.console==k);
      return `<h3 class="group">${esc(rhCon(k).label)}<span>${g.length}</span></h3>`+g.map(x=>rhCard(x,tracked)).join("")}).join("");
  }else $("#rhList").innerHTML=list.map(x=>rhCard(x,tracked)).join("");
}
$("#rhQ").oninput=renderRh;$("#rhCon").onchange=renderRh;
$("#rhSort").onchange=()=>{try{localStorage.setItem("sedotdeck.rhsort",$("#rhSort").value)}catch(e){}renderRh()};
$("#rhChips").onclick=e=>{const b=e.target.closest("button");if(!b)return;if(b.dataset.deck)rhDeck=!rhDeck;else rhTier=b.dataset.tier;renderRh()};
$("#rhInfo").onclick=e=>{if(e.target.id=="rhRefresh"){e.preventDefault();RH.items=null;loadRh(true)}};
$("#rhList").onclick=async e=>{
  const b=e.target.closest("[data-rh]");if(!b)return;
  const x=RH.items.find(i=>i.id==b.dataset.rh);if(!x)return;
  rhAdding.add(x.id);renderRh();
  try{await api("/api/add",{repo:"https://github.com/"+x.repo,name:x.title,rh_id:x.id});toast(`${x.title} added to My apps, checking for its release…`);await refresh()}
  catch(err){toast(err.message,true)}
  rhAdding.delete(x.id);renderRh();
};

// ---- Quiver catalog (quiverlauncher.com)
const AI_USE={none:"No AI",assisted:"AI-assisted",generated:"AI-generated"};
let QV={items:null,site:"https://quiverlauncher.com/",consoles:{}},qvAi="all",qvLoading=false,qvAdding=new Set(),qvKey="";
try{$("#qvSort").value=localStorage.getItem("sedotdeck.qvsort")||"added"}catch(e){}
async function loadQv(refresh){
  if(qvLoading||(QV.items&&!refresh))return renderQv();
  qvLoading=true;$("#qvInfo").textContent="Loading the catalog from quiverlauncher.com…";
  try{QV=await api("/api/quiver",{refresh})}catch(e){$("#qvInfo").textContent=e.message}
  qvLoading=false;renderQv();
}
const qvCon=k=>QV.consoles[k]||k||"Other";
const QV_SORT={
  added:(a,b)=>b.added-a.added,
  released:(a,b)=>b.released-a.released,
  recommended:(a,b)=>b.recommended-a.recommended||a.broken-b.broken||b.added-a.added,
  stars:(a,b)=>b.stars-a.stars||b.added-a.added,
  system:(a,b)=>qvCon(a.console).localeCompare(qvCon(b.console))||a.title.localeCompare(b.title),
  name:(a,b)=>a.title.localeCompare(b.title),
};
function qvCard(x,tracked){
  const added=tracked.has(x.key),busy=qvAdding.has(x.id);
  const initials=x.title.replace(/^the\s+/i,"").split(/\s+/).slice(0,2).map(w=>w[0]).join("").toUpperCase();
  const rep=[x.recommended?`<b>${x.recommended}</b> recommend`:"",x.broken?`<b>${x.broken}</b> report it broken`:""].filter(Boolean).join(" · ");
  return `<div class="rh-card">
    <div class="rh-cover">${esc(initials)}${x.cover?`<img src="${esc(x.cover)}" alt="" loading="lazy" onerror="this.remove()">`:""}</div>
    <div class="rh-body">
      <div class="rh-title">${esc(x.title)}</div>
      <div class="rh-meta"><span class="tag" title="${esc(qvCon(x.console))}">${esc((x.console||"?").toUpperCase())}</span>${esc(x.project)} · <span class="mono">${esc(short(x.version))}</span>${AI_USE[x.ai]?" · "+AI_USE[x.ai]:""}${x.stars>0?`<span class="rh-stars" title="${x.stars.toLocaleString()} GitHub stars">★ ${stars(x.stars)}</span>`:""}</div>
      ${x.summary?`<div class="rh-req" title="${esc(x.summary)}">${esc(x.summary)}</div>`:""}
      ${rep?`<div class="qv-rep">${rep}</div>`:""}
      <div class="rh-act">${added?'<span class="added">In My apps</span>':`<button class="primary" data-qv="${esc(x.id)}" ${busy?"disabled":""}>${busy?"Adding…":"Add"}</button>`}
        <a href="${esc(QV.site)}apps/${encodeURIComponent(x.id)}" target="_blank" rel="noopener">Details ↗</a></div>
    </div></div>`;
}
function renderQv(){
  if(!QV.items)return;
  qvKey=S.apps.map(a=>a.host+"/"+a.repo).join();
  const tracked=new Set(S.apps.map(a=>(a.host+"/"+a.repo).toLowerCase()));
  const sel=$("#qvCon"),cur=sel.value,per={};
  QV.items.forEach(x=>per[x.console]=(per[x.console]||0)+1);
  sel.innerHTML='<option value="">All systems</option>'+Object.keys(per).sort((a,b)=>qvCon(a).localeCompare(qvCon(b)))
    .map(k=>`<option value="${esc(k)}">${esc(qvCon(k))} (${per[k]})</option>`).join("");
  sel.value=per[cur]?cur:"";
  const q=$("#qvQ").value.trim().toLowerCase(),con=sel.value,sort=$("#qvSort").value;
  const list=QV.items.filter(x=>(qvAi=="all"||x.ai==qvAi)&&(!con||x.console==con)
    &&(!q||[x.title,x.project,x.developer,x.repo,qvCon(x.console),x.console].join(" ").toLowerCase().includes(q))).sort(QV_SORT[sort]||QV_SORT.added);
  const n=k=>QV.items.filter(x=>x.ai==k).length;
  $("#qvChips").innerHTML=[["all","Any AI use",QV.items.length],["none","No AI",n("none")],["assisted","AI-assisted",n("assisted")],["generated","AI-generated",n("generated")]]
    .map(([k,t,c])=>`<button class="${qvAi==k?"on":""}" data-ai="${k}" aria-pressed="${qvAi==k}">${t}<b>${c}</b></button>`).join("");
  const when=QV.fetched_at?`Updated ${ago(QV.fetched_at)}`:"";
  $("#qvInfo").innerHTML=`${list.length} of ${QV.items.length} apps with a Linux release · ${when} · <a href="#" id="qvRefresh">Refresh</a>`+(QV.stale?` · ${esc(QV.stale)}`:"");
  if(!list.length){$("#qvList").innerHTML='<div class="empty" style="grid-column:1/-1"><b>Nothing here</b>No apps match these filters. Clear the search or pick All systems.</div>';return}
  if(sort=="system"&&!con){
    const groups=[...new Set(list.map(x=>x.console))];
    $("#qvList").innerHTML=groups.map(k=>{const g=list.filter(x=>x.console==k);
      return `<h3 class="group">${esc(qvCon(k))}<span>${g.length}</span></h3>`+g.map(x=>qvCard(x,tracked)).join("")}).join("");
  }else $("#qvList").innerHTML=list.map(x=>qvCard(x,tracked)).join("");
}
$("#qvQ").oninput=renderQv;$("#qvCon").onchange=renderQv;
$("#qvSort").onchange=()=>{try{localStorage.setItem("sedotdeck.qvsort",$("#qvSort").value)}catch(e){}renderQv()};
$("#qvChips").onclick=e=>{const b=e.target.closest("button");if(!b)return;qvAi=b.dataset.ai;renderQv()};
$("#qvInfo").onclick=e=>{if(e.target.id=="qvRefresh"){e.preventDefault();QV.items=null;loadQv(true)}};
$("#qvList").onclick=async e=>{
  const b=e.target.closest("[data-qv]");if(!b)return;
  const x=QV.items.find(i=>i.id==b.dataset.qv);if(!x)return;
  qvAdding.add(x.id);renderQv();
  const host=x.provider=="gitlab"?"https://gitlab.com/":"https://github.com/";
  try{await api("/api/add",{repo:host+x.repo,name:x.title,qv_id:x.id});toast(`${x.title} added to My apps, checking for its release…`);await refresh()}
  catch(err){toast(err.message,true)}
  qvAdding.delete(x.id);renderQv();
};

// ---- Recomp Launcher catalog (recomplauncher.com)
let RL={items:null,site:"https://recomplauncher.com/",consoles:{}},rlLoading=false,rlAdding=new Set(),rlKey="";
try{$("#rlSort").value=localStorage.getItem("sedotdeck.rlsort")||"stars"}catch(e){}
async function loadRl(refresh){
  if(rlLoading||(RL.items&&!refresh))return renderRl();
  rlLoading=true;$("#rlInfo").textContent="Loading the catalog from recomplauncher.com…";
  try{RL=await api("/api/recomplauncher",{refresh})}catch(e){$("#rlInfo").textContent=e.message}
  rlLoading=false;renderRl();
}
const rlCon=k=>RL.consoles[k]||k||"Other";
const RL_SORT={
  stars:(a,b)=>b.stars-a.stars,
  released:(a,b)=>String(b.released).localeCompare(String(a.released))||b.stars-a.stars,
  added:(a,b)=>String(b.added).localeCompare(String(a.added))||b.stars-a.stars,
  system:(a,b)=>rlCon(a.console).localeCompare(rlCon(b.console))||a.title.localeCompare(b.title),
  name:(a,b)=>a.title.localeCompare(b.title),
};
function rlCard(x,tracked){
  const added=tracked.has(x.key),busy=rlAdding.has(x.id);
  const initials=x.title.replace(/^the\s+/i,"").split(/\s+/).slice(0,2).map(w=>w[0]).join("").toUpperCase();
  return `<div class="rh-card">
    <div class="rh-cover">${esc(initials)}${x.cover?`<img src="${esc(x.cover)}" alt="" loading="lazy" onerror="this.remove()">`:""}</div>
    <div class="rh-body">
      <div class="rh-title">${esc(x.title)}</div>
      <div class="rh-meta"><span class="tag" title="${esc(rlCon(x.console))}">${esc((x.console||"?").toUpperCase())}</span>${x.game?esc(x.game)+" · ":""}<span class="mono">${esc(short(x.version))}</span>${x.stars>0?`<span class="rh-stars" title="${x.stars.toLocaleString()} GitHub stars">★ ${stars(x.stars)}</span>`:""}</div>
      ${x.summary?`<div class="rh-req" title="${esc(x.summary)}">${esc(x.summary)}</div>`:""}
      <div class="rh-act">${added?'<span class="added">In My apps</span>':`<button class="primary" data-rl="${esc(x.id)}" ${busy?"disabled":""}>${busy?"Adding…":"Add"}</button>`}
        <a href="${esc(RL.site)}game/${encodeURIComponent(x.id)}" target="_blank" rel="noopener">Details ↗</a></div>
    </div></div>`;
}
function renderRl(){
  if(!RL.items)return;
  rlKey=S.apps.map(a=>a.host+"/"+a.repo).join();
  const tracked=new Set(S.apps.map(a=>(a.host+"/"+a.repo).toLowerCase()));
  const sel=$("#rlCon"),cur=sel.value,per={};
  RL.items.forEach(x=>per[x.console]=(per[x.console]||0)+1);
  sel.innerHTML='<option value="">All systems</option>'+Object.keys(per).sort((a,b)=>rlCon(a).localeCompare(rlCon(b)))
    .map(k=>`<option value="${esc(k)}">${esc(rlCon(k))} (${per[k]})</option>`).join("");
  sel.value=per[cur]?cur:"";
  const q=$("#rlQ").value.trim().toLowerCase(),con=sel.value,sort=$("#rlSort").value;
  const list=RL.items.filter(x=>(!con||x.console==con)
    &&(!q||[x.title,x.game,x.developer,x.repo,rlCon(x.console),x.console].join(" ").toLowerCase().includes(q))).sort(RL_SORT[sort]||RL_SORT.stars);
  const when=RL.fetched_at?`Updated ${ago(RL.fetched_at)}`:"";
  $("#rlInfo").innerHTML=`${list.length} of ${RL.items.length} recomps with a Linux release · ${when} · <a href="#" id="rlRefresh">Refresh</a>`+(RL.stale?` · ${esc(RL.stale)}`:"");
  if(!list.length){$("#rlList").innerHTML='<div class="empty" style="grid-column:1/-1"><b>Nothing here</b>No recomps match these filters. Clear the search or pick All systems.</div>';return}
  if(sort=="system"&&!con){
    const groups=[...new Set(list.map(x=>x.console))];
    $("#rlList").innerHTML=groups.map(k=>{const g=list.filter(x=>x.console==k);
      return `<h3 class="group">${esc(rlCon(k))}<span>${g.length}</span></h3>`+g.map(x=>rlCard(x,tracked)).join("")}).join("");
  }else $("#rlList").innerHTML=list.map(x=>rlCard(x,tracked)).join("");
}
$("#rlQ").oninput=renderRl;$("#rlCon").onchange=renderRl;
$("#rlSort").onchange=()=>{try{localStorage.setItem("sedotdeck.rlsort",$("#rlSort").value)}catch(e){}renderRl()};
$("#rlInfo").onclick=e=>{if(e.target.id=="rlRefresh"){e.preventDefault();RL.items=null;loadRl(true)}};
$("#rlList").onclick=async e=>{
  const b=e.target.closest("[data-rl]");if(!b)return;
  const x=RL.items.find(i=>i.id==b.dataset.rl);if(!x)return;
  rlAdding.add(x.id);renderRl();
  try{await api("/api/add",{repo:"https://github.com/"+x.repo,name:x.title,rl_id:x.id});toast(`${x.title} added to My apps, checking for its release…`);await refresh()}
  catch(err){toast(err.message,true)}
  rlAdding.delete(x.id);renderRl();
};

// ---- Discover: find a project on GitHub by the game's name
let GH={items:null,q:"",total:0},ghBusy=false,ghKey="";
$("#ghSort").onchange=()=>{if(GH.items)$("#ghForm").requestSubmit()};
$("#ghForm").onsubmit=async e=>{
  e.preventDefault();const q=$("#ghQ").value.trim();if(!q||ghBusy)return;
  ghBusy=true;$("#ghInfo").textContent=`Searching GitHub for “${q}”…`;
  try{GH=await api("/api/github_search",{q,sort:$("#ghSort").value});GH.q=q;renderGh()}
  catch(err){$("#ghInfo").textContent=err.message}
  ghBusy=false;
};
function renderGh(){
  if(!GH.items)return;
  ghKey=S.apps.map(a=>a.host+"/"+a.repo).join();
  const tracked=new Set(S.apps.filter(a=>a.host=="github.com").map(a=>a.repo.toLowerCase()));
  $("#ghInfo").textContent=GH.items.length?`${GH.total.toLocaleString()} projects match “${GH.q}”`+(GH.total>GH.items.length?`, showing the first ${GH.items.length}.`:"."):`Nothing on GitHub matches “${GH.q}”.`;
  $("#ghList").innerHTML=GH.items.map(r=>{const added=tracked.has(r.repo.toLowerCase());
    return `<div class="app repo-row"><div><div class="name">${esc(r.name)}</div><a class="repo" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.repo)}</a>
      ${r.description?`<div class="desc">${esc(r.description)}</div>`:""}
      <div class="meta"><span class="stars" title="${r.stars.toLocaleString()} stars">★ ${stars(r.stars)}</span>${r.language?`<span>${esc(r.language)}</span>`:""}${r.pushed_at?`<span>updated ${ago(Date.parse(r.pushed_at)/1000)}</span>`:""}${r.archived?'<span class="tag">archived</span>':""}</div></div>
      <div class="actions">${added?'<span class="added">In My apps</span>':`<button class="primary" data-gh="${esc(r.repo)}">Add</button>`}</div></div>`}).join("");
}
// Add opens the usual dialog, which looks up the latest release and the file it would download.
$("#ghList").onclick=e=>{const b=e.target.closest("[data-gh]");if(!b)return;openAdd(null);$("#aRepo").value=b.dataset.gh;preview()};

// ---- SedotDeck's own updates: asked once per start, unless that version was skipped
let SELF=null;
async function checkSelf(manual){
  try{SELF=await api("/api/self_update_check",{refresh:!!manual})}catch(e){if(manual)toast(e.message,true);return}
  $("#sSelfInfo").textContent=SELF.newer?`· ${SELF.latest} available`:"· up to date";
  if(SELF.newer&&(manual||!SELF.skipped))openSelf();
  else if(manual)toast(`SedotDeck ${SELF.current} is the newest version`);
}
// The dialog shows one of four views: the offer (selfMode null), "progress", "done" or "error".
let selfMode=null, selfTag="", selfErr="", selfS=[];
const mbs=b=>((b||0)/1e6).toFixed(1);
const selfCan=()=>SELF.method=="appimage"||SELF.method=="script";
function openSelf(){
  if(S.jobs.__self__&&S.jobs.__self__.state=="running")selfMode="progress";
  selfPaint();
  if(!dSelf.open)dSelf.showModal();
}
function selfPaint(){
  const m=selfMode, offer=!m, prog=m=="progress", done=m=="done", err=m=="error", j=S.jobs.__self__||{};
  $("#suTitle").textContent=prog?`Downloading SedotDeck ${SELF.latest}`:done?`SedotDeck ${selfTag} is installed`
    :err?"The update didn't finish":`SedotDeck ${SELF.latest} is available`;
  $("#suMeta").innerHTML=`You have ${esc(SELF.current)}`+(SELF.published?` · released ${esc(SELF.published.slice(0,10))}`:"")
    +` · <a href="${esc(SELF.url)}" target="_blank" rel="noopener">Release page</a>`
    +(SELF.method=="decky"?" · Update the Decky plugin from Decky's settings.":"");
  const body=$("#suBody");
  if(offer)body.innerHTML=md(SELF.notes||"")||'<span class="dim">No release notes.</span>';
  else if(prog){
    const sp=selfSpeed(), d=j.done||0, t=j.total||0;
    const txt=(t?`${mbs(d)} of ${mbs(t)} MB`:`${mbs(d)} MB downloaded`)+(sp?` · ${mbs(sp)} MB/s`:"");
    body.innerHTML=`<div class="su-prog"><div class="bar"><i style="transform:scaleX(${(j.progress||0).toFixed(3)})"></i></div><div class="jobtxt">${txt}</div>`
      +`<div class="hint">You can keep using SedotDeck while it downloads. Closing the window doesn't stop it: the download finishes first.</div></div>`;
  }
  else body.textContent=done?"Restart SedotDeck to use it.":selfErr;
  for(const id of ["#suSkip","#suLater","#suGo"])$(id).hidden=!offer;
  $("#suGo").textContent=selfCan()?"Update now":"Open release page";
  $("#suClose").hidden=!err;
  $("#suPrimary").hidden=offer;
  $("#suPrimary").textContent=prog?"Keep using SedotDeck":done?"Close":"Try again";
}
// Speed is the change over the last ~3 s of polls, so one slow poll does not make the number jump.
function selfSample(d){
  const now=Date.now();
  if(!selfS.length||selfS[selfS.length-1].d!=d)selfS.push({t:now,d});
  selfS=selfS.filter(s=>now-s.t<=3000);
}
function selfSpeed(){
  if(selfS.length<2)return 0;
  const a=selfS[0],b=selfS[selfS.length-1];
  return b.t>a.t?(b.d-a.d)/((b.t-a.t)/1000):0;
}
async function startSelf(){
  selfMode="progress";selfS=[];selfPaint();
  try{await api("/api/self_update",{});refresh()}
  catch(e){selfMode="error";selfErr=e.message;selfPaint()}
}
// Called once when the download job ends: the open dialog changes view, a closed one leaves a toast or the header note.
function selfEnd(j){
  const ok=j.state=="done";
  if(ok&&SELF)selfTag=SELF.latest;
  if(dSelf.open){selfMode=ok?"done":"error";selfErr=j.message||"";selfPaint()}
  else{selfMode=ok?"done":null;toast(j.message,!ok)}
}
dSelf.addEventListener("close",()=>{if(selfMode=="error")selfMode=null});
$("#suGo").onclick=()=>{if(selfCan())startSelf();else{dSelf.close();openUrl(SELF.url)}};
$("#suSkip").onclick=()=>{dSelf.close();act("/api/self_update_skip",{tag:SELF.latest},`Skipped ${SELF.latest}. You'll be asked again for the next version.`)};
$("#suLater").onclick=()=>dSelf.close();
$("#suClose").onclick=()=>dSelf.close();
$("#suPrimary").onclick=()=>{if(selfMode=="error")startSelf();else dSelf.close()};
$("#sSelf").onclick=()=>{dSet.close();checkSelf(true)};

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
    const a=document.createElement("a");a.href=url;a.download="sedotdeck-apps-"+data.exported_at.slice(0,10)+".json";
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
$("#sQuit").onclick=async()=>{try{await api("/api/quit")}catch(e){}document.body.innerHTML='<div class="empty"><b>SedotDeck closed</b>You can close this tab.</div>'};
// ---- Controller and D-pad: focus moves to the nearest control in the pressed direction
const FOCUSABLE='a[href],button:not(:disabled),input:not([type=hidden]):not(:disabled),select:not(:disabled),textarea,summary,[tabindex]:not([tabindex="-1"])';
const topDialog=()=>{const d=document.querySelectorAll("dialog[open]");return d[d.length-1]||null};
// The topmost open dialog, else the row menu when it's open, else the page.
function navScope(){return topDialog()||(menu.hidden?document:menu)}
function shown(el){
  if(el.closest("dialog:not([open])"))return false;
  if(el.checkVisibility){if(!el.checkVisibility())return false}
  else if(!el.getClientRects().length||el.offsetParent===null&&getComputedStyle(el).position!="fixed")return false;
  // A closed <details> hides its body; only the summary stays reachable.
  for(let d=el.closest("details:not([open])");d;d=d.parentElement&&d.parentElement.closest("details:not([open])")){
    const s=d.querySelector(":scope>summary");
    if(!s||!s.contains(el))return false;
  }
  return true;
}
const navList=()=>[...navScope().querySelectorAll(FOCUSABLE)].filter(shown);
function focusEl(el){el.focus({preventScroll:true});el.scrollIntoView({block:"nearest",inline:"nearest"})}
// Nothing in scope has focus yet: start on the active tab, else the first control.
function navStart(list){
  const tab=navScope()===document&&document.querySelector("#tabs .on");
  const el=list.includes(tab)?tab:list[0];
  if(el)focusEl(el);
}
function navMove(dir){
  const list=navList(),cur=document.activeElement;
  if(!list.length)return;
  if(!list.includes(cur)){navStart(list);return}
  const h=dir=="left"||dir=="right",s=dir=="right"||dir=="down"?1:-1;
  const r=cur.getBoundingClientRect(),c=h?r.left+r.width/2:r.top+r.height/2;
  const k1=h?"left":"top",k2=h?"right":"bottom",a1=h?r.top:r.left,a2=h?r.bottom:r.right;
  let best=null,score=Infinity;
  for(const el of list){
    if(el===cur)continue;
    const q=el.getBoundingClientRect(),ahead=s*((s>0?q[k1]:q[k2])-c);
    if(ahead<-1)continue;
    const b1=h?q.top:q.left,b2=h?q.bottom:q.right,v=Math.max(0,ahead)+2*Math.max(0,b1-a2,a1-b2);
    if(v<score){score=v;best=el}
  }
  if(best)focusEl(best);
  else if(navScope()===document&&!h)scrollBy(0,s*innerHeight*0.6);
}
const ARROWS={ArrowUp:"up",ArrowDown:"down",ArrowLeft:"left",ArrowRight:"right"};
const isText=el=>!!el&&(el.tagName=="TEXTAREA"||el.tagName=="INPUT"&&/^(text|search|url|number|password|email)$/.test(el.type));
// Left/Right step a select and keep the caret in a text field; everything else moves focus.
// Returns false when the key should keep its normal browser behavior.
function dirInput(dir){
  const el=document.activeElement,h=dir=="left"||dir=="right";
  if(h&&el&&el.tagName=="SELECT"){
    const i=el.selectedIndex+(dir=="right"?1:-1);
    if(i>=0&&i<el.options.length){el.selectedIndex=i;el.dispatchEvent(new Event("change",{bubbles:true}))}
    return true;
  }
  if(h&&isText(el))return false;
  navMove(dir);
  return true;
}
document.addEventListener("keydown",e=>{
  const dir=ARROWS[e.key];
  if(!dir||e.ctrlKey||e.altKey||e.metaKey||e.shiftKey||!menu.hidden)return;  // the row menu handles its own keys
  if(dirInput(dir))e.preventDefault();
});

// ---- Gamepad, standard mapping. Polled only while a pad is connected.
let padRaf=0,padPrev=[],padHeld="",padNext=0;
const PAD_DIRS=["up","down","left","right"];  // buttons 12 to 15
function padRead(){
  const list=[...(navigator.getGamepads?.()||[])].filter(Boolean);
  return list.find(p=>p.mapping=="standard")||list[0]||null;
}
// The D-pad, else the left stick (the dominant axis wins past a 0.5 deadzone).
function padDirOf(p){
  const k=[12,13,14,15].find(k=>p.buttons[k]&&p.buttons[k].pressed);
  if(k)return PAD_DIRS[k-12];
  const x=p.axes[0]||0,y=p.axes[1]||0;
  if(Math.max(Math.abs(x),Math.abs(y))<=0.5)return"";
  if(Math.abs(x)>Math.abs(y))return x<0?"left":"right";
  return y<0?"up":"down";
}
function padButton(i){
  const el=document.activeElement;
  if(i==0){  // A: press the focused control
    if(!navList().includes(el)){navStart(navList());return}
    if(el.tagName=="SELECT"){try{el.showPicker()}catch(e){}}
    else if(!isText(el))el.click();
  }
  else if(i==1){  // B: back, one level at a time
    const d=topDialog();
    if(!menu.hidden)closeMenu();
    // Same path as Escape, so ask() sees the close and resolves as cancelled.
    else if(d){if(d.requestClose)d.requestClose();else if(d.dispatchEvent(new Event("cancel",{cancelable:true})))d.close()}
    else if(SEL)setSel(false);
    else if(el&&el.closest("main section"))focusEl($("#tabs .on"));
  }
  else if(i==2){  // X: the row menu of the focused app (not while selecting)
    const card=el&&el.closest("[data-card]"),more=card&&!SEL&&card.querySelector("[data-a=menu]");
    if(more)more.click();
  }
  else if(i==3){  // Y: search on the current tab
    const q={gh:"#ghQ",rh:"#rhQ",qv:"#qvQ",rl:"#rlQ"}[TAB];
    if(q&&!topDialog())focusEl($(q));
  }
  else if(i==4||i==5){  // LB / RB: previous or next tab
    if(topDialog())return;
    const tabs=[...$("#tabs").querySelectorAll("[data-tab]")],at=tabs.findIndex(b=>b.dataset.tab==TAB);
    const t=tabs[(at+(i==5?1:-1)+tabs.length)%tabs.length];
    setTab(t.dataset.tab);focusEl(t);
  }
  else if(i==9){if(!topDialog())$("#btnSet").click()}  // Start: Settings
}
function padScroll(dy){const d=topDialog();if(d)d.scrollTop+=dy;else scrollBy(0,dy)}
// The hint strip shows while a pad is in use, and not while Select mode has its own bar.
function padUi(){
  const h=$("#padHint"),want=document.documentElement.classList.contains("pad")&&$("#selbar").hidden;
  if(h.hidden==want)h.hidden=!want;
}
function padStart(){if(!padRaf)padRaf=requestAnimationFrame(padFrame)}
function padFrame(t){
  const p=padRead();
  if(!p){padRaf=0;padPrev=[];padHeld="";$("#padHint").hidden=true;return}
  padRaf=requestAnimationFrame(padFrame);
  const b=Array.from(p.buttons,x=>!!(x&&x.pressed));
  let used=false;
  // Buttons act once per press.
  for(const i of [0,1,2,3,4,5,9])if(b[i]&&!padPrev[i]){used=true;padButton(i)}
  padPrev=b;
  // Directions repeat: first after 350 ms, then every 110 ms.
  const d=padDirOf(p);
  if(d&&d!=padHeld){padHeld=d;padNext=t+350;used=true;dirInput(d)}
  else if(d&&t>=padNext){padNext=t+110;dirInput(d)}
  else if(!d)padHeld="";
  if(Math.abs(p.axes[3]||0)>0.25){used=true;padScroll(p.axes[3]*24)}  // right stick scrolls
  if(used)document.documentElement.classList.add("pad");
  padUi();
}
addEventListener("gamepadconnected",padStart);
addEventListener("gamepaddisconnected",padStart);
addEventListener("mousedown",()=>document.documentElement.classList.remove("pad"));
addEventListener("pointermove",e=>{if(e.movementX||e.movementY)document.documentElement.classList.remove("pad")});
padStart();

refresh();
</script></body></html>
"""


if __name__ == "__main__":
    main()
