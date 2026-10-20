"""Offline tests for deckport.py. Usage: python3 -I tests/test_deckport.py deckport.py"""
import hashlib, importlib.util, io, json, os, sys, tempfile, time, zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("deckport", sys.argv[1])
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)
ok = 0


def eq(a, b, what):
    global ok
    assert a == b, "%s: %r != %r" % (what, a, b)
    ok += 1


# --- parse_source
eq(dp.parse_source("HarbourMasters/Shipwright"), ("github", "github.com", "HarbourMasters/Shipwright"), "bare")
eq(dp.parse_source("https://github.com/a/b/releases/tag/v1"), ("github", "github.com", "a/b"), "gh url")
eq(dp.parse_source("git@github.com:a/b.git"), ("github", "github.com", "a/b"), "ssh")
eq(dp.parse_source("https://gitlab.com/veloren/veloren/-/releases"), ("gitlab", "gitlab.com", "veloren/veloren"), "gitlab")
eq(dp.parse_source("gitlab.com/group/sub/proj"), ("gitlab", "gitlab.com", "group/sub/proj"), "gitlab subgroup")
eq(dp.parse_source("https://codeberg.org/forgejo/forgejo/releases"), ("gitea", "codeberg.org", "forgejo/forgejo"), "codeberg")
eq(dp.parse_source("https://git.example.org/me/game/releases"), (None, "git.example.org", "me/game"), "unknown host")
eq(dp.parse_source("https://git.example.org/me/game/-/releases"), ("gitlab", "git.example.org", "me/game"), "selfhosted gitlab")
for bad in ("http://github.com/a/b", "https://github.com/onlyowner", "nonsense"):
    try:
        dp.parse_source(bad); raise AssertionError("should fail: " + bad)
    except ValueError:
        ok += 1

# --- asset picking
A = lambda *names: [{"name": n, "id": i, "browser_download_url": "https://x/" + n} for i, n in enumerate(names)]
eq(dp.pick_asset(A("Linux (aarch64)", "Linux (x86_64)", "MacOS (x86_64)", "Windows (x86_64)"))["name"], "Linux (x86_64)", "veloren")
eq(dp.pick_asset(A("forgejo-17-linux-arm-6", "forgejo-17-linux-amd64", "forgejo-17-linux-amd64.sha256", "forgejo-17-linux-arm64"))["name"],
   "forgejo-17-linux-amd64", "forgejo")
eq(dp.pick_asset(A("Z-Linux-ARM64.zip", "Z-Linux-Flatpak-X64.zip", "Z-Linux-X64.zip", "Z-macOS.zip"))["name"], "Z-Linux-X64.zip", "zelda")
eq(dp.score_asset("armor-linux.AppImage") > 0, True, "armor is not arm")

# --- GitHub digest normalization + checksum-file detection
g = dp._norm_github({"tag_name": "v1", "assets": [{"id": 1, "name": "a.AppImage", "browser_download_url": "https://x/a",
                                                    "digest": "sha256:" + "AB" * 32}]})
eq(g["assets"][0]["sha256"], "ab" * 32, "digest")
rel = {"tag_name": "v1", "assets": A("game-linux.AppImage", "SHA256SUMS.txt", "game-linux.AppImage.sha512")}
info = dp.release_info(rel, rel["assets"][0])
eq(info["asset"]["checksum_url"], "https://x/SHA256SUMS.txt", "generic sums file")
rel = {"tag_name": "v1", "assets": A("f-linux-amd64", "f-linux-amd64.sha256")}
eq(dp.release_info(rel, rel["assets"][0])["asset"]["checksum_specific"], True, "specific sums file")

# --- fake network for install tests
FILES = {}
dp._open_download = lambda url: _Resp(FILES[url])


class _Resp(io.BytesIO):
    def __init__(self, b):
        super().__init__(b)
        self.headers = {"Content-Length": str(len(b))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


appimage = b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\x00" * 600
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as z:
    z.writestr("Game-v2/game.AppImage", appimage)
zipped = zbuf.getvalue()
FILES["https://x/Linux (x86_64)"] = zipped
FILES["https://x/sums"] = ("%s  Linux (x86_64)\n%s  other\n" % (hashlib.sha256(zipped).hexdigest(), "0" * 64)).encode()

tmp = tempfile.mkdtemp()
settings = {"install_dir": tmp, "keep_backup": True}


def mkapp(sha="", checksum_url=None):
    asset = {"id": 7, "name": "Linux (x86_64)", "size": len(zipped), "updated_at": "t",
             "download_url": "https://x/Linux (x86_64)", "sha256": sha}
    if checksum_url:
        asset["checksum_url"] = checksum_url
    return {"name": "My Game", "latest": {"tag": "v2", "asset": asset}}

# correct checksum via sums file; zip label without extension still becomes an AppImage
app = mkapp(checksum_url="https://x/sums")
path = dp.install_app(app, settings)
eq(path, os.path.join(tmp, "My-Game.AppImage"), "installed path")
eq(app["installed"]["verified"], True, "verified via sums")
eq(os.access(path, os.X_OK), True, "executable")

# wrong published digest: refuse, keep the old file
before = Path(path).read_bytes()
app = mkapp(sha="f" * 64)
try:
    dp.install_app(app, settings); raise AssertionError("mismatch should fail")
except RuntimeError as e:
    eq("SHA-256 mismatch" in str(e), True, "mismatch message")
eq(Path(path).read_bytes(), before, "old file untouched")
eq([f for f in os.listdir(tmp) if f.startswith(".deckport-")], [], "temp cleaned up")

# no checksum anywhere: installs, flagged unverified
app = mkapp()
dp.install_app(app, settings)
eq(app["installed"]["verified"], False, "unverified")
eq(os.path.exists(path + ".old"), True, "backup kept")

# --- export / import
cfg = {"settings": {"github_token": "SECRET"}, "apps": []}
added, skipped = dp.import_apps(cfg, {"format": "deckport-apps", "apps": [
    {"name": "A", "provider": "github", "host": "github.com", "repo": "a/b", "pattern": "", "prerelease": False},
    {"name": "V", "provider": "gitlab", "host": "gitlab.com", "repo": "veloren/veloren"},
    {"name": "dup", "provider": "github", "host": "github.com", "repo": "A/B"},
]})
eq((len(added), skipped), (2, 1), "import merge")
exp = dp.export_apps(cfg)
eq("SECRET" in json.dumps(exp), False, "no token in export")
eq(dp.import_apps(cfg, exp), ([], 2), "re-import is a no-op")
for bad in ({"apps": []}, {"format": "deckport-apps", "apps": [{"provider": "evil", "host": "x.y", "repo": "a/b"}]},
            {"format": "deckport-apps", "apps": [{"provider": "github", "host": "github.com", "repo": "../../etc"}]},
            {"format": "deckport-apps", "apps": [{"provider": "github", "host": "github.com", "repo": "a/b", "pattern": "("}]}):
    n = len(cfg["apps"])
    try:
        dp.import_apps(cfg, bad); raise AssertionError("should reject %r" % bad)
    except ValueError:
        eq(len(cfg["apps"]), n, "rejected import leaves list unchanged")

# --- old (GitHub-only) config migrates
d = tempfile.mkdtemp()
dp.CONFIG_FILE = Path(d) / "config.json"
dp.CONFIG_FILE.write_text(json.dumps({"apps": [{"id": "x", "name": "Old", "repo": "o/r"}]}))
c = dp.load_config()
eq((c["apps"][0]["provider"], c["apps"][0]["host"]), ("github", "github.com"), "migration")
eq(dp.web_url(c["apps"][0]), "https://github.com/o/r/releases", "web url")

# --- a running Server picks up changes another DeckPort saved (desktop UI <-> Decky plugin)
srv = dp.Server()
eq([a["name"] for a in srv.handle("/api/state", {})["apps"]], ["Old"], "server sees config")
time.sleep(0.01)
other = dp.load_config()
other["apps"].append(dict(other["apps"][0], id="y", name="FromPlugin"))
dp.save_config(other)
eq([a["name"] for a in srv.handle("/api/state", {})["apps"]], ["Old", "FromPlugin"], "server reloads changed config")

# --- desktop-app mode: the way the Tauri shell runs the server
import re, subprocess, urllib.error, urllib.request
home = tempfile.mkdtemp()
env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))
Path(env["XDG_CONFIG_HOME"], "deckport").mkdir(parents=True)
Path(env["XDG_CONFIG_HOME"], "deckport", "config.json").write_text(json.dumps(
    {"settings": {"github_token": "SECRET"}, "apps": [{"id": "x", "name": "A", "repo": "a/b"}]}))
proc = subprocess.Popen([sys.executable, "-I", sys.argv[1], "serve", "--no-browser", "--port", "0", "--exit-on-stdin-close"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
line = proc.stdout.readline().strip()
eq(line.startswith("DECKPORT_READY http://127.0.0.1:") and not line.endswith(":0/"), True, "ready line with real port")
base = line.split(" ", 1)[1]
html = urllib.request.urlopen(base + "?app=1", timeout=5).read().decode()
token = re.search(r'const TOKEN="([^"]+)"', html).group(1)


def post(path, body):
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-DeckPort-Token": token})
    try:
        return 200, json.loads(urllib.request.urlopen(req, timeout=5).read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

code, r = post("/api/export_save", {})
eq(code, 200, "export_save ok")
saved = Path(r["path"])
eq((saved.parent == Path(home, "Downloads"), "SECRET" in saved.read_text(), r["count"]), (True, False, 1), "export saved without token")
eq(post("/api/export_save", {})[1]["path"] != r["path"], True, "second export doesn't overwrite")
for bad in ("http://example.com", "file:///etc/passwd", "javascript:alert(1)", 5):
    eq(post("/api/open_url", {"url": bad})[0], 400, "open_url rejects %r" % (bad,))
proc.stdin.close()
try:
    eq(proc.wait(timeout=5), 0, "server exits when stdin closes")
except subprocess.TimeoutExpired:
    proc.kill(); raise AssertionError("server did not exit after stdin closed")

print("all %d checks passed" % ok)
