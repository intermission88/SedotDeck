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

# --- the install path is pinned by the first install; renaming the app never moves it
app = mkapp(); app["name"] = "Pin One"
p1 = dp.install_app(app, settings)
eq(app["target_path"], p1, "target_path pinned after install")
app["name"] = "Renamed Thing"
eq(dp.install_app(app, settings), p1, "rename keeps the installed path")
eq(os.path.exists(os.path.join(tmp, "Renamed-Thing.AppImage")), False, "no file under the new name")

legacy = os.path.join(tmp, "Legacy Name.AppImage")
Path(legacy).write_bytes(appimage)
app = mkapp(); app["name"] = "Something Else"; app["installed"] = {"path": legacy, "kind": "appimage"}
eq(dp.install_app(app, settings), legacy, "installed.path alone pins the update")
eq(app["target_path"], legacy, "legacy config migrated to target_path")

abuf = io.BytesIO()
with zipfile.ZipFile(abuf, "w") as z:
    z.writestr("Port-v1/bin/port", b"#!/bin/sh\n")
FILES["https://x/port.zip"] = abuf.getvalue()
def mkarch():
    return {"name": "Port Game", "latest": {"tag": "v1", "asset": {"id": 9, "name": "port.zip", "size": 1, "updated_at": "t",
            "download_url": "https://x/port.zip", "sha256": ""}}}
app = mkarch()
d1 = dp.install_app(app, settings)
eq(os.path.isfile(os.path.join(d1, "bin", "port")), True, "archive folder installed")
eq("target_path" in app, False, "folder is not a target_path")
app["name"] = "Port Renamed"
eq(dp.install_app(app, settings), d1, "archive folder keeps its path after rename")
eq(os.path.exists(os.path.join(tmp, "Port-Renamed")), False, "no folder under the new name")

dpin = os.path.join(tmp, "AFolder"); os.mkdir(dpin)
app = mkapp(); app["name"] = "Dir Pinned"; app["installed"] = {"path": dpin, "kind": "archive"}
pa = dp.install_app(app, settings)
eq(pa, os.path.join(tmp, "Dir-Pinned.AppImage"), "dir pin + AppImage release falls back to name")
eq(os.path.isdir(dpin), True, "pinned folder untouched")

# --- folder apps: stable launcher, stale-file cleanup
def zip_of(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, (data, mode) in files.items():
            zi = zipfile.ZipInfo(n)
            zi.external_attr = mode << 16
            z.writestr(zi, data)
    return b.getvalue()


ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 200
def mkfolder(url, tag):
    return {"name": "Fold Game", "repo": "me/foldgame", "latest": {"tag": tag, "asset": {
        "id": 1, "name": "fold.zip", "size": 1, "updated_at": tag, "download_url": url, "sha256": ""}}}


FILES["https://x/fold1"] = zip_of({"FoldGame-v1/FoldGame-1.0.x86_64": (ELF, 0o755), "FoldGame-v1/libfoo.so.1": (ELF, 0o755),
                                   "FoldGame-v1/lib/helper": (ELF * 50, 0o755), "FoldGame-v1/old.dat": (b"old", 0o644),
                                   "FoldGame-v1/data/a/old.txt": (b"x", 0o644), "FoldGame-v1/data/keep.txt": (b"k", 0o644)})
FILES["https://x/fold2"] = zip_of({"FoldGame-v2/FoldGame-2.0.x86_64": (ELF, 0o755), "FoldGame-v2/data/keep.txt": (b"k2", 0o644)})
ftmp = tempfile.mkdtemp()
fset = {"install_dir": ftmp, "keep_backup": True}
fapp = mkfolder("https://x/fold1", "v1")
fdir = dp.install_app(fapp, fset)
fl = fapp["launcher"]
eq(fl, os.path.join(ftmp, "Fold-Game.sh"), "launcher next to the folder")
eq(os.access(fl, os.X_OK), True, "launcher executable")
eq(Path(fl).read_text(), "#!/bin/sh\ncd %s || exit 1\nexec ./FoldGame-1.0.x86_64 \"$@\"\n" % fdir, "launcher content")
eq(sorted(fapp["installed"]["files"])[:2], ["FoldGame-1.0.x86_64", "data/a/old.txt"], "manifest recorded")
eq(dp.find_main_exe(fdir, ["fold"]), os.path.join(fdir, "FoldGame-1.0.x86_64"), "main exe skips libs")

# user save + rename + update with a new exe name
os.makedirs(os.path.join(fdir, "saves")); Path(fdir, "saves", "slot1.sav").write_text("save")
fapp["name"] = "Totally Renamed"
fapp["latest"] = mkfolder("https://x/fold2", "v2")["latest"]
eq(dp.install_app(fapp, fset), fdir, "folder path unchanged")
eq(fapp["launcher"], fl, "launcher path stays after rename")
eq("FoldGame-2.0.x86_64" in Path(fl).read_text() and "1.0" not in Path(fl).read_text(), True, "launcher repointed")
eq((os.path.exists(os.path.join(fdir, "FoldGame-1.0.x86_64")), os.path.exists(os.path.join(fdir, "old.dat")),
    os.path.isdir(os.path.join(fdir, "data", "a"))), (False, False, False), "stale release files and empty dirs removed")
eq((Path(fdir, "saves", "slot1.sav").read_text(), Path(fdir, "data", "keep.txt").read_text()), ("save", "k2"), "user files and new files stay")
eq(os.path.exists(os.path.join(fdir, "lib", "helper")), False, "old lib file removed too (it was in the old release)")

# no previous manifest: delete nothing
fapp2 = mkfolder("https://x/fold1", "v1"); fapp2["name"] = "Legacy Fold"
fdir2 = os.path.join(ftmp, "LegacyDir"); os.mkdir(fdir2); Path(fdir2, "stale.bin").write_text("s")
fapp2["installed"] = {"path": fdir2, "kind": "archive"}
dp.install_app(fapp2, fset)
eq(os.path.exists(os.path.join(fdir2, "stale.bin")), True, "no manifest, nothing deleted")
eq("files" in fapp2["installed"], True, "manifest starts with this install")

# an unrelated file at the launcher path is never overwritten
Path(ftmp, "Other-Fold.sh").write_text("mine")
fapp3 = mkfolder("https://x/fold1", "v1"); fapp3["name"] = "Other Fold"
dp.install_app(fapp3, fset)
eq((Path(ftmp, "Other-Fold.sh").read_text(), os.path.basename(fapp3["launcher"])), ("mine", "Other-Fold-1.sh"), "existing file kept")
# no executable found: no launcher
FILES["https://x/fold3"] = zip_of({"Data-v1/readme.txt": (b"hi", 0o644)})
fapp4 = mkfolder("https://x/fold3", "v1"); fapp4["name"] = "Docs Only"
dp.install_app(fapp4, fset)
eq("launcher" in fapp4, False, "no launcher without an executable")
# remove with files deletes the launcher
fapp["id"] = "f1"
fcfg = {"settings": fset, "apps": [fapp]}
dp.remove_app(fcfg, fapp, delete_files=True)
eq((os.path.exists(fl), os.path.exists(fdir)), (False, False), "launcher removed with the app")
# a folder app goes to Steam through its launcher
sv = Path(tempfile.mkdtemp(), "shortcuts.vdf")
eq(dp.add_to_steam(fapp3, sv), True, "folder app added to steam")
ent = dict((k_, v) for _t, k_, v in dp.vdf_load(sv.read_bytes())[0][2][0][2])
eq((ent["Exe"], ent["StartDir"]), ('"%s"' % fapp3["launcher"], '"%s"' % fapp3["installed"]["path"]), "steam uses launcher + app folder")
try:
    dp.add_to_steam(fapp4, sv); raise AssertionError("no launcher should fail")
except RuntimeError:
    ok += 1

# --- choosing the program of a folder app: manual pick, Windows .exe via Proton
MZ = b"MZ\x90\x00" + b"\x00" * 100
FILES["https://x/win1"] = zip_of({"WinGame/WinGame.exe": (MZ, 0o644), "WinGame/unins000.exe": (MZ * 9, 0o644),
                                  "WinGame/start.sh": (b"#!/bin/sh\necho\n", 0o644), "WinGame/data.pak": (b"d", 0o644)})
wapp = mkfolder("https://x/win1", "v1"); wapp["name"] = "Win Game"; wapp["repo"] = "me/wingame"
wdir = dp.install_app(wapp, fset)
names = [(c["file"], c["kind"]) for c in dp.launch_candidates(wdir, ["wingame"])]
eq(names, [("start.sh", "linux"), ("WinGame.exe", "windows"), ("unins000.exe", "windows")], "candidates: linux first, junk exe last")
eq(wapp["launch"], {"file": "start.sh", "kind": "linux", "manual": False}, "auto picks the script")
eq(os.access(os.path.join(wdir, "start.sh"), os.X_OK), True, "picked script made executable")
wl = wapp["launcher"]
eq(dp.steam_target(wapp), (wl, wdir, False), "steam starts the .sh launcher")
# the user picks the .exe: no .sh launcher, Steam starts the exe with Proton
dp.set_launch_file(wapp, fset, "WinGame.exe")
eq((wapp["launch"]["manual"], os.path.exists(wl)), (True, False), "manual exe pick, launcher removed")
eq(dp.steam_target(wapp), (os.path.join(wdir, "WinGame.exe"), wdir, True), "exe target needs proton")
try:
    dp.launch_app(wapp); raise AssertionError("exe should not launch directly")
except RuntimeError:
    ok += 1
try:
    dp.set_launch_file(wapp, fset, "../../etc/passwd"); raise AssertionError("outside file accepted")
except ValueError:
    ok += 1
try:
    dp.set_launch_file(wapp, fset, "data.pak"); raise AssertionError("non-program accepted")
except ValueError:
    ok += 1
# the pick survives an update
dp.install_app(wapp, fset)
eq(wapp["launch"], {"file": "WinGame.exe", "kind": "windows", "manual": True}, "manual pick kept across update")
# a picked file the new release dropped: back to automatic, with a notice
FILES["https://x/win2"] = zip_of({"WinGame/Game2.exe": (MZ, 0o644)})
wapp["latest"] = mkfolder("https://x/win2", "v2")["latest"]
dp.install_app(wapp, fset)
eq((wapp["launch"]["file"], wapp["launch"]["manual"], "gone" in wapp.get("notice", "")), ("Game2.exe", False, True),
   "dropped pick falls back to auto")
eq(os.path.exists(os.path.join(wdir, "start.sh")), False, "dropped script pruned")
# a Linux pick writes the .sh launcher again
Path(wdir, "run.sh").write_text("#!/bin/sh\n")
dp.set_launch_file(wapp, fset, "run.sh")
eq((os.path.isfile(wapp["launcher"]), dp.steam_target(wapp)[2]), (True, False), "linux pick writes launcher again")
dp.set_launch_file(wapp, fset, "")
eq(wapp["launch"]["manual"], False, "automatic again")
sv2 = Path(tempfile.mkdtemp(), "shortcuts.vdf")
dp.set_launch_file(wapp, fset, "Game2.exe")
eq(dp.add_to_steam(wapp, sv2), True, "exe added to steam")
ent = dict((k_, v) for _t, k_, v in dp.vdf_load(sv2.read_bytes())[0][2][0][2])
eq((ent["Exe"], ent["StartDir"]), ('"%s"' % os.path.join(wdir, "Game2.exe"), '"%s"' % wdir), "steam shortcut to the exe")

# --- rename_app_file: clean name, .old, Steam shortcut
rdir = tempfile.mkdtemp()
rf = os.path.join(rdir, "Eden-Linux-v0.0.3-amd64.AppImage")
Path(rf).write_bytes(appimage); Path(rf + ".old").write_bytes(b"old")
rapp = {"name": "Eden", "target_path": rf, "installed": {"path": rf, "kind": "appimage"}}
rsv = Path(tempfile.mkdtemp(), "shortcuts.vdf")
rsv.write_bytes(dp.vdf_dump([(0, "shortcuts", [
    (0, "0", [(2, "appid", -77), (1, "AppName", "Eden Emu"), (1, "Exe", '"%s"' % rf), (1, "StartDir", '"%s"' % rdir), (2, "LastPlayTime", 5)]),
    (0, "1", [(2, "appid", -78), (1, "AppName", "Other"), (1, "Exe", '"/x/other"'), (1, "StartDir", '"/x"')])])]))
rnew = dp.rename_app_file(rapp, rsv)
eq(rnew, os.path.join(rdir, "Eden.AppImage"), "clean name")
eq((os.path.exists(rf), os.path.exists(rf + ".old"), os.path.exists(rnew), os.path.exists(rnew + ".old")), (False, False, True, True), "file and .old renamed")
eq((rapp["target_path"], rapp["installed"]["path"]), (rnew, rnew), "paths updated")
sc = dp.vdf_load(rsv.read_bytes())[0][2]
e0 = dict((k_, v) for _t, k_, v in sc[0][2]); e1 = dict((k_, v) for _t, k_, v in sc[1][2])
eq((e0["Exe"], e0["StartDir"], e0["appid"], e0["AppName"], e0["LastPlayTime"]), ('"%s"' % rnew, '"%s"' % rdir, -77, "Eden Emu", 5), "shortcut rewritten, rest kept")
eq(e1["Exe"], '"/x/other"', "other shortcut untouched")
eq(Path(str(rsv) + ".deckport.bak").exists(), True, "shortcuts backup")
try:
    dp.rename_app_file(rapp, rsv); raise AssertionError("already named")
except RuntimeError:
    ok += 1
other = os.path.join(rdir, "Taken.AppImage"); Path(other).write_bytes(appimage)
Path(rdir, "Eden Beta.AppImage").write_bytes(appimage)
tapp = {"name": "Taken", "installed": {"path": os.path.join(rdir, "Eden Beta.AppImage"), "kind": "appimage"}}
try:
    dp.rename_app_file(tapp, rsv); raise AssertionError("target exists")
except RuntimeError as e:
    eq("already exists" in str(e) and os.path.exists(os.path.join(rdir, "Eden Beta.AppImage")), True, "refused when target exists")

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

# --- adopting an AppImage the user already has (versioned name, Steam shortcut pointing at it)
eq(dp.stem_of("Game-v1.1-x86_64.AppImage"), dp.stem_of("Game-v1.2-x86_64.AppImage"), "stem ignores version/arch")
eq(dp.stem_of("Game-v1.1-x86_64.AppImage"), "game", "stem value")
eq(dp.stem_of("Zelda64Recompiled-v1.2.2-Linux-X64.AppImage"), "zelda64recompiled", "stem keeps digits in names")
eq(dp.stem_of("OtherGame.AppImage") == "game", False, "different game")

adir = tempfile.mkdtemp()
for f in ("Game-v1.1-x86_64.AppImage", "Game-v1.1-x86_64.AppImage.old", "Other.AppImage", "Taken.AppImage", "notes.txt"):
    Path(adir, f).write_bytes(appimage)
os.symlink(os.path.join(adir, "Other.AppImage"), os.path.join(adir, "Link.AppImage"))
acfg = {"settings": {"install_dir": adir, "keep_backup": True},
        "apps": [{"id": "t", "name": "Taken", "repo": "x/taken", "installed": {"path": os.path.join(adir, "Taken.AppImage")}}]}
cands = dp.find_adopt_candidates(acfg, "Game", "someone/game", "Game-v1.2-x86_64.AppImage")
eq([c["name"] for c in cands], ["Game-v1.1-x86_64.AppImage", "Other.AppImage"], "candidates skip .old, symlinks, tracked")
eq([c["suggested"] for c in cands], [True, False], "best match suggested")
eq(dp.find_adopt_candidates(acfg, "Nope", "a/nope", "")[0]["suggested"], False, "no false suggestion")
for bad, why in ((os.path.join(adir, "Link.AppImage"), "symlink"), (os.path.join(adir, "notes.txt"), "not appimage"),
                 (os.path.join(adir, "Taken.AppImage"), "tracked"), (path, "outside install dir")):
    try:
        dp.validate_adopt_path(acfg, bad); raise AssertionError("adopt should reject " + why)
    except ValueError:
        ok += 1
old_file = os.path.join(adir, "Game-v1.1-x86_64.AppImage")
eq(dp.validate_adopt_path(acfg, old_file), os.path.realpath(old_file), "valid adopt path")

# same bytes as the latest release -> already current, verified, nothing to download
FILES["https://x/Game-v1.2-x86_64.AppImage"] = b"new release bytes" + appimage
def gapp(sha):
    return {"id": "g", "name": "Game", "latest": {"tag": "v1.2", "asset": {
        "id": 42, "name": "Game-v1.2-x86_64.AppImage", "size": 1, "updated_at": "u",
        "download_url": "https://x/Game-v1.2-x86_64.AppImage", "sha256": sha}}}
g = gapp(hashlib.sha256(appimage).hexdigest())
eq(dp.adopt_app(g, old_file), True, "adopt: hash matches latest")
eq((dp.has_update(g), g["installed"]["verified"], g["installed"]["tag"]), (False, True, "v1.2"), "adopted file up to date")
# older file -> update offered, and the update overwrites the SAME versioned path
g = gapp(hashlib.sha256(FILES["https://x/Game-v1.2-x86_64.AppImage"]).hexdigest())
eq(dp.adopt_app(g, old_file), False, "adopt: older file")
eq((dp.has_update(g), g["installed"]["tag"]), (True, "unknown"), "adopted old file needs update")
acfg["settings"]["install_dir"] = adir
eq(dp.install_app(g, acfg["settings"]), old_file, "update written to the adopted path")
eq(Path(old_file).read_bytes(), FILES["https://x/Game-v1.2-x86_64.AppImage"], "adopted file replaced in place")
eq(os.path.exists(old_file + ".old") and not os.path.exists(os.path.join(adir, "Game.AppImage")), True,
   "backup kept, no second Game.AppImage")

# --- no re-installing something that is already current
srv2 = dp.Server()
srv2.cfg = {"settings": acfg["settings"], "apps": [g]}
srv2.save = lambda: None
try:
    srv2.handle("/api/update", {"id": "g"}); raise AssertionError("re-install of current app should be refused")
except ValueError as e:
    eq("already up to date" in str(e), True, "reinstall refused")
os.remove(old_file)  # user deleted the file -> installing again is allowed
eq(srv2.handle("/api/update", {"id": "g"})["count"], 1, "missing file can be installed again")
while srv2.busy():
    time.sleep(0.05)
eq(os.path.exists(old_file), True, "reinstalled to the same path")

# --- asset picking fixes found on real releases (xemu dbg build, melonDS zipped AppImage, DuckStation SSE2)
eq(dp.pick_asset(A("xemu-1-dbg-x86_64.AppImage", "xemu-1-x86_64.AppImage"))["name"], "xemu-1-x86_64.AppImage", "no dbg build")
eq(dp.pick_asset(A("melonDS-1.1-macOS.zip", "melonDS-1.1-appimage-x86_64.zip", "melonDS-1.1-ubuntu-x86_64.zip"))["name"],
   "melonDS-1.1-appimage-x86_64.zip", "zipped appimage")
eq(dp.pick_asset(A("DuckStation-x64-SSE2.AppImage", "DuckStation-x64.AppImage"))["name"], "DuckStation-x64.AppImage", "not sse2")

# --- release notes travel with the latest release
g = dp._norm_github({"tag_name": "v2", "body": "## Fixes\n- crash", "assets": []})
eq(dp.release_info(g, None)["notes"], "## Fixes\n- crash", "github notes")
eq(dp._norm_gitlab({"tag_name": "v2", "description": "notes"}, "gitlab.com", "a/b")["body"], "notes", "gitlab notes")


# --- folder scan: name AppImages the user already has (embedded update info, then the catalog)
def fake_elf(upd):
    names = b"\0.shstrtab\0.upd_info\0"
    upd = upd.encode() + b"\0" * 8
    names_off, upd_off = 64, 64 + len(names)
    shoff = upd_off + len(upd)
    hdr = bytearray(64)
    hdr[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<Q", hdr, 0x28, shoff)
    struct.pack_into("<HHH", hdr, 0x3A, 64, 3, 1)
    sec = lambda name, off, size: struct.pack("<IIQQQQ", name, 1, 0, 0, off, size) + b"\0" * 24
    return bytes(hdr) + names + upd + b"\0" * 64 + sec(1, names_off, len(names)) + sec(11, upd_off, len(upd))

import struct
sdir = tempfile.mkdtemp()
Path(sdir, "weird-name.AppImage").write_bytes(fake_elf("gh-releases-zsync|hrydgard|ppsspp|latest|*.zsync"))
Path(sdir, "pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage").write_bytes(appimage)
Path(sdir, "Mystery-1.0.AppImage").write_bytes(appimage)
Path(sdir, "Hidden.AppImage").write_bytes(appimage)
Path(sdir, "DeckPort-1.5.0-x86_64.AppImage").write_bytes(appimage)
Path(sdir, "Cemu.AppImage").write_bytes(appimage)
eq(dp.read_update_info(os.path.join(sdir, "weird-name.AppImage")), "gh-releases-zsync|hrydgard|ppsspp|latest|*.zsync", "upd_info")
eq(dp.read_update_info(os.path.join(sdir, "Mystery-1.0.AppImage")), "", "no upd_info")
scfg = {"settings": {"install_dir": sdir, "scan_ignored": [os.path.join(sdir, "Hidden.AppImage")]}, "apps": [
    dp.new_app("", "My PCSX2", src={"provider": "github", "host": "github.com", "repo": "PCSX2/pcsx2"}),
    dict(dp.new_app("", "Cemu", src={"provider": "github", "host": "github.com", "repo": "cemu-project/Cemu"}),
         installed={"path": old_file})]}
found = {f["file"]: f for f in dp.scan_folder(scfg)}
eq(sorted(found), ["Mystery-1.0.AppImage", "pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage", "weird-name.AppImage"],
   "scan skips DeckPort itself, hidden files and spare copies of installed apps")
eq((found["weird-name.AppImage"]["src"], found["weird-name.AppImage"]["name"], found["weird-name.AppImage"]["how"]),
   ("hrydgard/ppsspp", "PPSSPP", "embedded"), "identified by update info")
eq((found["pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage"]["how"], found["pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage"]["app_id"]),
   ("catalog", scfg["apps"][0]["id"]), "catalog match offered for the tracked app")
eq(found["Mystery-1.0.AppImage"]["src"], "", "unknown file")

# --- added by link: a catalog app gets its catalog name, not the repo's ("rpcs3-binaries-linux")
eq(dp.new_app("https://github.com/RPCS3/rpcs3-binaries-linux")["name"], "RPCS3", "catalog name")
eq(dp.new_app("", src={"provider": "github", "host": "github.com", "repo": "me/my-port"})["name"], "my-port", "repo name")

# --- removing an app but keeping its file: the scan doesn't offer it straight back
cemu_file = os.path.join(sdir, "Cemu.AppImage")
rcfg = {"settings": {"install_dir": sdir}, "apps": [dict(dp.new_app("", "Cemu", src={"provider": "github", "host": "github.com",
                                                                                   "repo": "cemu-project/Cemu"}), installed={"path": cemu_file})]}
dp.remove_app(rcfg, rcfg["apps"][0])
eq(("Cemu.AppImage" in [f["file"] for f in dp.scan_folder(rcfg)], rcfg["settings"]["scan_ignored"]), (False, [cemu_file]), "removed file hidden")

# --- tracking found files is all-or-nothing
tsrv = dp.Server.__new__(dp.Server)
tsrv.cfg, tsrv.jobs = {"settings": {"install_dir": sdir}, "apps": []}, dp.Jobs()
tsrv.sync = tsrv.save = lambda: None
try:
    tsrv.handle("/api/track_found", {"items": [{"path": os.path.join(sdir, "pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage"), "src": "PCSX2/pcsx2"},
                                               {"path": os.path.join(sdir, "nope.AppImage"), "src": "a/b"}]})
    raise AssertionError("bad item should fail")
except ValueError:
    pass
eq(tsrv.cfg["apps"], [], "nothing tracked when one item is bad")

# --- skip this version
k = {"latest": {"tag": "v3", "asset": {"id": 3, "updated_at": "t3"}},
     "installed": {"path": old_file, "asset_id": 2, "updated_at": "t2"}, "skip": {"asset_id": 3, "updated_at": "t3"}}
eq((dp.has_update(k), dp.has_update(k, honor_skip=False)), (False, True), "skipped version hidden")
k["latest"]["asset"] = {"id": 4, "updated_at": "t4"}
eq(dp.has_update(k), True, "next version shown again")

# --- Steam shortcuts.vdf: keep what's there, add ours once
vdf_path = Path(tempfile.mkdtemp(), "shortcuts.vdf")
existing = [(0, "shortcuts", [(0, "0", [(2, "appid", -123), (1, "AppName", "Other"), (1, "Exe", '"/x/other"'),
                                        (7, "big", 2 ** 40), (0, "tags", [(1, "0", "fav")])])])]
vdf_path.write_bytes(dp.vdf_dump(existing))
eq(dp.vdf_load(vdf_path.read_bytes()), existing, "vdf round trip")
sapp = {"name": "Game", "installed": {"path": old_file}}
eq(dp.add_to_steam(sapp, vdf_path), True, "added to steam")
eq(dp.add_to_steam(sapp, vdf_path), False, "not added twice")
lst = dp.vdf_load(vdf_path.read_bytes())[0][2]
new = dict((k_, v) for _t, k_, v in lst[1][2])
eq((len(lst), lst[0][2], lst[1][1], new["Exe"], new["AppName"]), (2, existing[0][2][0][2], "1", '"%s"' % old_file, "Game"),
   "existing shortcut kept, ours appended")
eq(new["appid"] < 0 and (new["appid"] + (1 << 32)) & 0x80000000 != 0, True, "shortcut appid has the high bit")
eq(Path(str(vdf_path) + ".deckport.bak").exists(), True, "vdf backup")

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
