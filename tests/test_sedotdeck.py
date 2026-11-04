"""Offline tests for sedotdeck.py. Usage: python3 -I tests/test_sedotdeck.py sedotdeck.py"""
import hashlib, importlib.util, io, json, os, sys, tempfile, time, zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("sedotdeck", sys.argv[1])
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
eq(path, os.path.join(tmp, "game.AppImage"), "installed path: the AppImage's own name")
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
eq([f for f in os.listdir(tmp) if f.startswith(".sedotdeck-")], [], "temp cleaned up")

# cancel mid-download: the old file stays, nothing is left behind
app = mkapp()
app["target_path"] = path
def cancel_now(done, total):
    raise dp.Cancelled()
try:
    dp.install_app(app, settings, cancel_now); raise AssertionError("cancel should stop the install")
except dp.Cancelled:
    ok += 1
eq(Path(path).read_bytes(), before, "cancelled: old file untouched")
eq([f for f in os.listdir(tmp) if f.startswith(".sedotdeck-")], [], "cancelled: download folder removed")

# a job that is cancelled ends as "cancelled", not as an error
jobs = dp.Jobs()
def slow(job):
    for _ in range(200):
        if job["cancel"]:
            raise dp.Cancelled()
        time.sleep(0.01)
jobs.start("c", "Downloading", slow)
eq(jobs.cancel("c"), True, "cancel accepted while running")
for _ in range(200):
    if jobs.snapshot()["c"]["state"] != "running":
        break
    time.sleep(0.01)
eq((jobs.snapshot()["c"]["state"], jobs.snapshot()["c"]["message"]), ("cancelled", "Download cancelled"), "job ends cancelled")
eq(jobs.cancel("c"), False, "nothing to cancel once it ended")
eq(jobs.cancel("nope"), False, "unknown job")

# no checksum anywhere: installs, flagged unverified
app = mkapp()
app["target_path"] = path  # the same app updating
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
eq(fl, os.path.join(fdir, dp.LAUNCHER_NAME), "launcher inside the app folder")
eq(dp.LAUNCHER_NAME in [c["file"] for c in dp.launch_candidates(fdir)], False, "own launcher is never a candidate")
eq(os.access(fl, os.X_OK), True, "launcher executable")
eq(Path(fl).read_text(), "#!/bin/sh\ncd %s || exit 1\nexec ./FoldGame-1.0.x86_64 \"$@\"\n" % fdir, "launcher content")
eq(sorted(fapp["installed"]["files"])[:2], ["FoldGame-1.0.x86_64", "data/a/old.txt"], "manifest recorded")
eq(dp.find_main_exe(fdir, ["fold"]), os.path.join(fdir, "FoldGame-1.0.x86_64"), "main exe skips libs")

# user save + rename + update with a new exe name
os.makedirs(os.path.join(fdir, "saves")); Path(fdir, "saves", "slot1.sav").write_text("save")
fapp["name"] = "Totally Renamed"
fapp["latest"] = mkfolder("https://x/fold2", "v2")["latest"]
eq(dp.install_app(fapp, fset), fdir, "folder path unchanged")
eq((fapp["launcher"], os.path.isfile(fl)), (fl, True), "launcher stays after rename and update")
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

# a launcher from before, next to the folder, moves inside unless a Steam shortcut still points at it
fapp3 = mkfolder("https://x/fold1", "v1"); fapp3["name"] = "Other Fold"
legacy = os.path.join(ftmp, "Other-Fold.sh"); Path(legacy).write_text("old")
fapp3["launcher"] = legacy
real_exes, dp.steam_shortcut_exes = dp.steam_shortcut_exes, lambda: {legacy}
dp.install_app(fapp3, fset)
eq((fapp3["launcher"], "exec ./" in Path(legacy).read_text()), (legacy, True), "launcher a Steam shortcut uses stays put")
dp.steam_shortcut_exes = lambda: set()
dp.install_app(fapp3, fset)
eq((os.path.dirname(fapp3["launcher"]), os.path.exists(legacy)), (fapp3["installed"]["path"], False), "unused old launcher moved inside")
dp.steam_shortcut_exes = real_exes
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

# --- rename_app_file: the AppImage's own name without its version, .old, Steam shortcut
rdir = tempfile.mkdtemp()
rf = os.path.join(rdir, "Eden-Linux-v0.0.3-amd64.AppImage")
Path(rf).write_bytes(appimage); Path(rf + ".old").write_bytes(b"old")
rapp = {"name": "Eden", "target_path": rf, "installed": {"path": rf, "kind": "appimage"}}
rsv = Path(tempfile.mkdtemp(), "shortcuts.vdf")
rsv.write_bytes(dp.vdf_dump([(0, "shortcuts", [
    (0, "0", [(2, "appid", -77), (1, "AppName", "Eden Emu"), (1, "Exe", '"%s"' % rf), (1, "StartDir", '"%s"' % rdir), (2, "LastPlayTime", 5)]),
    (0, "1", [(2, "appid", -78), (1, "AppName", "Other"), (1, "Exe", '"/x/other"'), (1, "StartDir", '"/x"')])])]))
rnew = dp.rename_app_file(rapp, rsv)
eq(rnew, os.path.join(rdir, "Eden-Linux-amd64.AppImage"), "file name without the version")
eq((os.path.exists(rf), os.path.exists(rf + ".old"), os.path.exists(rnew), os.path.exists(rnew + ".old")), (False, False, True, True), "file and .old renamed")
eq((rapp["target_path"], rapp["installed"]["path"]), (rnew, rnew), "paths updated")
sc = dp.vdf_load(rsv.read_bytes())[0][2]
e0 = dict((k_, v) for _t, k_, v in sc[0][2]); e1 = dict((k_, v) for _t, k_, v in sc[1][2])
eq((e0["Exe"], e0["StartDir"], e0["appid"], e0["AppName"], e0["LastPlayTime"]), ('"%s"' % rnew, '"%s"' % rdir, -77, "Eden Emu", 5), "shortcut rewritten, rest kept")
eq(e1["Exe"], '"/x/other"', "other shortcut untouched")
eq(Path(str(rsv) + ".sedotdeck.bak").exists(), True, "shortcuts backup")
try:
    dp.rename_app_file(rapp, rsv); raise AssertionError("already named")
except RuntimeError:
    ok += 1
other = os.path.join(rdir, "Taken.AppImage"); Path(other).write_bytes(appimage)
Path(rdir, "Eden Beta.AppImage").write_bytes(appimage)
tapp = {"name": "Taken", "installed": {"path": os.path.join(rdir, "Eden Beta.AppImage"), "kind": "appimage"}}
try:
    dp.rename_app_file(tapp, rsv, name="Taken"); raise AssertionError("target exists")
except RuntimeError as e:
    eq("already exists" in str(e) and os.path.exists(os.path.join(rdir, "Eden Beta.AppImage")), True, "refused when target exists")

# custom name: .AppImage added, Steam shortcut follows, and the next update writes to it
cnew = dp.rename_app_file(rapp, rsv, name="  Eden ")
eq(cnew, os.path.join(rdir, "Eden.AppImage"), "custom name gets .AppImage")
eq(dict((k_, v) for _t, k_, v in dp.vdf_load(rsv.read_bytes())[0][2][0][2])["Exe"], '"%s"' % cnew, "shortcut follows custom name")
eq(dp.pinned_path(rapp), cnew, "updates write to the custom name")
for bad in ("../x", "a/b", ".hidden", "", "   .AppImage"):
    try:
        dp.clean_file_name(bad); raise AssertionError("bad name accepted: %r" % bad)
    except RuntimeError:
        ok += 1

# auto rename after an update: only when switched on, never over another file
adir2 = tempfile.mkdtemp()
def ai(fn, asset):
    p_ = os.path.join(adir2, fn); Path(p_).write_bytes(appimage)
    return {"name": fn, "target_path": p_, "installed": {"path": p_, "kind": "appimage", "asset": asset}}
sw = ai("Zelda.AppImage", "Savage_Wind-0.4.1-alpha.4-x86_64.AppImage")
eq(dp.auto_rename(sw, {"auto_rename": False}), "", "auto rename off: name kept")
eq(dp.auto_rename(sw, {"auto_rename": True}), os.path.join(adir2, "Savage_Wind-x86_64.AppImage"), "auto rename on")
eq(dp.auto_rename(sw, {"auto_rename": True}), "", "already named: nothing to do")
blocker = ai("Old-Name.AppImage", "Savage_Wind-0.5-x86_64.AppImage")
eq(dp.auto_rename(blocker, {"auto_rename": True}), "", "name taken: no rename, no error")

# rename all: renames what it can, reports the rest
gow = ai("God-of-War-III.AppImage", "spartanport-x86_64.AppImage")
done_, failed_ = dp.rename_all([sw, blocker, gow], rsv)
eq(done_, ["spartanport-x86_64.AppImage"], "rename all renamed the free one")
eq([n for n, _e in failed_], ["Old-Name.AppImage"], "rename all reports the taken one")

# --- version numbers come out of AppImage names; arch tags and names with digits stay
for src, want in (("Savage_Wind-0.4.1-alpha.4-x86_64.AppImage", "Savage_Wind-x86_64.AppImage"),
                  ("spartanport-x86_64.AppImage", "spartanport-x86_64.AppImage"),
                  ("Eden-Linux-v0.0.3-amd64.AppImage", "Eden-Linux-amd64.AppImage"),
                  ("Zelda64Recompiled-v1.2.2-Linux-X64.AppImage", "Zelda64Recompiled-Linux-X64.AppImage"),
                  ("pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage", "pcsx2-linux-appimage-x64-Qt.AppImage"),
                  ("Game_v5_x86_64.AppImage", "Game_x86_64.AppImage"),
                  ("Nightly-2026-01-02-x86_64.AppImage", "Nightly-x86_64.AppImage"),
                  ("Ship2Harkinian.AppImage", "Ship2Harkinian.AppImage"),
                  ("rpcs3-v0.0.34-17345-6f3e1bba_linux64.AppImage", "rpcs3_linux64.AppImage"),
                  ("SpartanPort-nightly-a1b2c3d-x86_64.AppImage", "SpartanPort-nightly-x86_64.AppImage"),
                  ("Dolphin-2412-x86_64.AppImage", "Dolphin-x86_64.AppImage"),
                  ("Mario-64-x86_64.AppImage", "Mario-64-x86_64.AppImage"),
                  ("Cafe-deadbeef-x86_64.AppImage", "Cafe-deadbeef-x86_64.AppImage"),
                  ("1.2.3.AppImage", "")):
    eq(dp.unversioned_name(src), want, "unversioned " + src)

# --- export / import
cfg = {"settings": {"github_token": "SECRET"}, "apps": []}
added, skipped = dp.import_apps(cfg, {"format": "sedotdeck-apps", "apps": [
    {"name": "A", "provider": "github", "host": "github.com", "repo": "a/b", "pattern": "", "prerelease": False},
    {"name": "V", "provider": "gitlab", "host": "gitlab.com", "repo": "veloren/veloren"},
    {"name": "dup", "provider": "github", "host": "github.com", "repo": "A/B"},
]})
eq((len(added), skipped), (2, 1), "import merge")
exp = dp.export_apps(cfg)
eq("SECRET" in json.dumps(exp), False, "no token in export")
eq(dp.import_apps(cfg, exp), ([], 2), "re-import is a no-op")
for bad in ({"apps": []}, {"format": "sedotdeck-apps", "apps": [{"provider": "evil", "host": "x.y", "repo": "a/b"}]},
            {"format": "sedotdeck-apps", "apps": [{"provider": "github", "host": "github.com", "repo": "../../etc"}]},
            {"format": "sedotdeck-apps", "apps": [{"provider": "github", "host": "github.com", "repo": "a/b", "pattern": "("}]}):
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

# --- a running Server picks up changes another SedotDeck saved (desktop UI <-> Decky plugin)
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


# --- folder scan: name AppImages the user already has (embedded update info)
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
Path(sdir, "SedotDeck-1.5.0-x86_64.AppImage").write_bytes(appimage)
Path(sdir, "Cemu.AppImage").write_bytes(fake_elf("gh-releases-zsync|cemu-project|Cemu|latest|*.zsync"))
eq(dp.read_update_info(os.path.join(sdir, "weird-name.AppImage")), "gh-releases-zsync|hrydgard|ppsspp|latest|*.zsync", "upd_info")
eq(dp.read_update_info(os.path.join(sdir, "Mystery-1.0.AppImage")), "", "no upd_info")
scfg = {"settings": {"install_dir": sdir, "scan_ignored": [os.path.join(sdir, "Hidden.AppImage")]}, "apps": [
    dp.new_app("", "My PCSX2", src={"provider": "github", "host": "github.com", "repo": "PCSX2/pcsx2"}),
    dict(dp.new_app("", "Cemu", src={"provider": "github", "host": "github.com", "repo": "cemu-project/Cemu"}),
         installed={"path": old_file})]}
found = {f["file"]: f for f in dp.scan_folder(scfg)}
eq(sorted(found), ["Mystery-1.0.AppImage", "pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage", "weird-name.AppImage"],
   "scan skips SedotDeck itself, hidden files and spare copies of installed apps")
eq((found["weird-name.AppImage"]["src"], found["weird-name.AppImage"]["name"]),
   ("hrydgard/ppsspp", "ppsspp"), "identified by update info")
eq(found["pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage"]["src"], "", "a file name alone doesn't identify a repo")
eq(found["Mystery-1.0.AppImage"]["src"], "", "unknown file")

# --- added by link: named after the repo
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
eq(Path(str(vdf_path) + ".sedotdeck.bak").exists(), True, "vdf backup")

# --- desktop-app mode: the way the Tauri shell runs the server
import re, subprocess, urllib.error, urllib.request
home = tempfile.mkdtemp()
env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"))
Path(env["XDG_CONFIG_HOME"], "sedotdeck").mkdir(parents=True)
Path(env["XDG_CONFIG_HOME"], "sedotdeck", "config.json").write_text(json.dumps(
    {"settings": {"github_token": "SECRET"}, "apps": [{"id": "x", "name": "A", "repo": "a/b"}]}))
proc = subprocess.Popen([sys.executable, "-I", sys.argv[1], "serve", "--no-browser", "--port", "0", "--exit-on-stdin-close"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
line = proc.stdout.readline().strip()
eq(line.startswith("SEDOTDECK_READY http://127.0.0.1:") and not line.endswith(":0/"), True, "ready line with real port")
base = line.split(" ", 1)[1]
html = urllib.request.urlopen(base + "?app=1", timeout=5).read().decode()
token = re.search(r'const TOKEN="([^"]+)"', html).group(1)


def post(path, body):
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-SedotDeck-Token": token})
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

# --- release choice: skip titles, Latest only, moved repos
def ghrel(tag, name="", pre=False, repo="me/game"):
    return {"tag_name": tag, "name": name or tag, "prerelease": pre, "draft": False,
            "html_url": "https://github.com/%s/releases/tag/%s" % (repo, tag), "assets": []}
ROUTES = {}
def fake_json(url, token="", service=""):
    if url not in ROUTES:
        raise LookupError("not found")
    return ROUTES[url]
real_http_json, dp.http_json = dp.http_json, fake_json
GH = "https://api.github.com/repos/me/game/releases"
gsrc = {"provider": "github", "host": "github.com", "repo": "me/game"}
ROUTES[GH + "/latest"] = ghrel("nightly-123", "Nightly build")
ROUTES[GH + "?per_page=30"] = [ghrel("nightly-123", "Nightly build"), ghrel("v2.0"), ghrel("v1.0")]
eq(dp.fetch_release(gsrc, False)["tag_name"], "nightly-123", "default: GitHub's Latest")
eq(dp.fetch_release(gsrc, False, skip_titles="nightly")["tag_name"], "v2.0", "skipped title falls back to the list")
try:
    dp.fetch_release(gsrc, False, skip_titles="nightly", latest_only=True); raise AssertionError("latest_only + skipped latest")
except RuntimeError as e:
    eq("skip filter" in str(e), True, "latest_only says why")
del ROUTES[GH + "/latest"]
try:
    dp.fetch_release(gsrc, False, latest_only=True); raise AssertionError("no Latest marker")
except RuntimeError as e:
    eq("no release marked Latest" in str(e), True, "latest_only without a Latest release")
ROUTES[GH + "?per_page=30"] = [ghrel("nightly-1")]
try:
    dp.fetch_release(gsrc, False, skip_titles="nightly"); raise AssertionError("everything skipped")
except RuntimeError as e:
    eq("after skipping" in str(e), True, "all releases skipped")
h = "ec667e25f9ab82ea32a10e745ff48583f63cd1d4"
pgo = [{"name": "eglantine-%s-linux-%s.AppImage" % (h, v)} for v in ("x86_64", "aarch64-pgo", "x86_64-pgo")]
eq(dp.pick_asset(pgo)["name"], pgo[2]["name"], "PGO build wins a tie whatever the order")
# a repo with git tags but no releases: the newest version tag's source archive
TAGS = "https://api.github.com/repos/me/game/tags?per_page=100&page="
ROUTES[GH + "?per_page=15"] = []
ROUTES[TAGS + "1"] = [
    {"name": "v1.10", "commit": {"sha": "a10"}}, {"name": "v2.0-beta.1", "commit": {"sha": "b2"}},
    {"name": "v1.9", "commit": {"sha": "a9"}}, {"name": "snapshot", "commit": {"sha": "s1"}}]
trel = dp.fetch_release(gsrc, False)
eq((trel["tag_name"], trel["assets"][0]["browser_download_url"], trel["assets"][0]["id"]),
   ("v1.10", "https://github.com/me/game/archive/refs/tags/v1.10.tar.gz", "a10"), "tags only: newest stable tag")
eq(dp.fetch_release(gsrc, True)["tag_name"], "snapshot", "tags only: a rolling tag is the newest pre-release")
eq(dp.pick_release_asset(trel)["name"], "game-v1.10.tar.gz", "tags only: source archive is the pick")
eq(dp.pick_release_asset(trel, "linux"), None, "tags only: a pattern still has to match")
# tags sorted by name: the newest version can be on the second page
ROUTES[TAGS + "1"] = [{"name": "v1.%d" % n, "commit": {"sha": "p%d" % n}} for n in range(99, -1, -1)]
ROUTES[TAGS + "2"] = [{"name": "v1.100", "commit": {"sha": "p100"}}]
eq(dp.fetch_release(gsrc, False)["tag_name"], "v1.100", "tags only: more than one page read")
del ROUTES[TAGS + "2"]
# a build-name suffix isn't a pre-release; a real one is
ROUTES[TAGS + "1"] = [{"name": "v2.0-rc1"}, {"name": "v1.5-linux"}, {"name": "nightly"}, {"name": "v0.1"}]
eq((dp.fetch_release(gsrc, False)["tag_name"], dp.fetch_release(gsrc, True)["tag_name"]),
   ("v1.5-linux", "nightly"), "tags only: -linux is stable, -rc1 and nightly are pre-releases")
# check_app: no releases once means tags only for a day, one request per check
calls = []
def counting(url, token="", service=""):
    calls.append(url)
    return fake_json(url, token, service)
dp.http_json = counting
tapp = dp.new_app("me/game")
dp.check_app(tapp)
eq((tapp["error"], tapp["latest"]["tag"], dp.has_update(tapp), bool(tapp.get("tags_only_at"))),
   ("", "v1.5-linux", True, True), "tags only: check finds an update and remembers")
calls.clear()
dp.check_app(tapp)
eq(calls, [TAGS + "1"], "tags only: the next check reads only the tags")
tapp["tags_only_at"] -= dp.TAGS_RECHECK + 1
calls.clear()
dp.check_app(tapp)
eq(GH + "?per_page=15" in calls, True, "tags only: releases looked at again after a day")
tapp["tags_only_at"] = "garbage"
dp.check_app(tapp)
eq(tapp["error"], "", "tags only: a broken timestamp is ignored")
tapp["pattern"] = "x86_64"
dp.check_app(tapp)
eq("publishes no releases" in tapp["error"], True, "tags only: the error says there are no releases")
ROUTES[GH + "?per_page=15"] = [dict(ghrel("v3.0"), assets=[{"id": 1, "name": "game-x86_64.AppImage", "browser_download_url": "https://x/g"}])]
tapp["tags_only_at"] -= dp.TAGS_RECHECK + 1
dp.check_app(tapp)
eq((tapp["latest"]["tag"], "tags_only_at" in tapp), ("v3.0", False), "tags only: a real release ends it")
dp.http_json = fake_json
ROUTES[GH + "?per_page=15"] = []
ROUTES[TAGS + "1"] = []
try:
    dp.fetch_release(gsrc, False); raise AssertionError("no releases, no tags")
except RuntimeError as e:
    eq("No releases or tags found" in str(e), True, "tags only: none at all")
del ROUTES[GH + "?per_page=15"]
# Gitea/Forgejo: Latest only uses /releases/latest, otherwise the newest in the list
CB = "https://codeberg.org/api/v1/repos/me/game/releases"
csrc = {"provider": "gitea", "host": "codeberg.org", "repo": "me/game"}
ROUTES[CB + "/latest"] = dict(ghrel("v1.0"), html_url="https://codeberg.org/me/game/releases/tag/v1.0")
ROUTES[CB + "?limit=15"] = [dict(ghrel("v1.1"), html_url="https://codeberg.org/me/game/releases/tag/v1.1"), ROUTES[CB + "/latest"]]
eq((dp.fetch_release(csrc, False)["tag_name"], dp.fetch_release(csrc, False, latest_only=True)["tag_name"]),
   ("v1.1", "v1.0"), "gitea newest vs Latest")
# a renamed repo: GitHub redirects the API, and the release's own link names the new repo
mapp = dp.new_app("me/game")
dp.apply_options(mapp, {"pattern": "", "skip_titles": "", "latest_only": False})
ROUTES[GH + "/latest"] = dict(ghrel("v3", repo="newowner/Game"), assets=[{"id": 9, "name": "game-linux.AppImage",
                                                                         "browser_download_url": "https://x/g"}])
dp.check_app(mapp)
eq(mapp.get("moved_to"), "newowner/Game", "moved repo detected")
ROUTES[GH + "/latest"] = dict(ROUTES[GH + "/latest"], html_url="https://github.com/ME/GAME/releases/tag/v3")
dp.check_app(mapp)
eq("moved_to" in mapp, False, "case change alone isn't a move, and the notice clears")
dp.http_json = real_http_json

# --- per-app options: validation, export/import round trip, cleared values
oapp = dp.new_app("me/opts")
dp.apply_options(oapp, {"skip_titles": "nightly|beta", "latest_only": 1, "category": "  Ports   and  recomps "})
eq((oapp["skip_titles"], oapp["latest_only"], oapp["category"]), ("nightly|beta", True, "Ports and recomps"), "options set")
try:
    dp.apply_options(oapp, {"skip_titles": "(", "category": "X"}); raise AssertionError("bad regex accepted")
except ValueError:
    eq(oapp["category"], "Ports and recomps", "nothing changed on a bad option")
ocfg = {"settings": {}, "apps": [oapp]}
exp = dp.export_apps(ocfg)
eq({k: exp["apps"][0].get(k) for k in ("skip_titles", "latest_only", "category")},
   {"skip_titles": "nightly|beta", "latest_only": True, "category": "Ports and recomps"}, "options exported")
added, _skipped = dp.import_apps({"settings": {}, "apps": []}, exp)
eq((added[0]["skip_titles"], added[0]["latest_only"], added[0]["category"]), ("nightly|beta", True, "Ports and recomps"), "options imported")
bad = json.loads(json.dumps(exp)); bad["apps"][0]["skip_titles"] = "("
try:
    dp.import_apps({"settings": {}, "apps": []}, bad); raise AssertionError("bad import accepted")
except ValueError as e:
    eq("Entry 1" in str(e), True, "import names the bad entry")
dp.apply_options(oapp, {"skip_titles": "", "latest_only": False, "category": ""})
eq([k for k in ("skip_titles", "latest_only", "category") if k in oapp], [], "cleared options are dropped")

# --- Recomp Haven: what SedotDeck lists from its data file
RH_JS = """/* header */
window.RECOMPHAVEN_META = {"snapshot": "2026-10-23"};

window.RECOMPS = [
  {"id": "b-game", "title": "B Game", "repo": "https://github.com/b/b", "version": "v1", "quality": "B", "stars": 5,
   "platforms": ["windows", "linux"], "features": ["steam-deck"], "cover": "assets/covers/b-game.webp", "console": "n64"},
  {"id": "s-game", "title": "S Game", "repo": "https://github.com/s/s", "version": "v2", "quality": "S", "stars": 1,
   "platforms": ["linux"], "cover": "javascript:alert(1)"},
  {"id": "roms", "title": "Has ROMs", "repo": "https://github.com/r/r", "version": "v1", "quality": "A", "platforms": ["linux"], "romFiles": ["x.z64"]},
  {"id": "win", "title": "Windows only", "repo": "https://github.com/w/w", "version": "v1", "quality": "A", "platforms": ["windows"]},
  {"id": "norel", "title": "No release", "repo": "https://github.com/n/n", "version": null, "quality": "D", "platforms": ["linux"]},
  {"id": "gl", "title": "Elsewhere", "repo": "https://gitlab.com/g/g", "version": "v1", "quality": "A", "platforms": ["linux"]}
];
"""
rh = dp.parse_recomphaven(RH_JS)
eq([x["id"] for x in rh], ["s-game", "b-game"], "linux releases from GitHub only, no ROM hosts, best tier first")
eq((rh[1]["repo"], rh[1]["key"], rh[1]["deck"], rh[1]["windows"]), ("b/b", "github.com/b/b", True, True), "entry fields")
eq((rh[1]["cover"], rh[0]["cover"]), ("https://recomphaven.com/assets/covers/b-game.webp", ""), "only real cover paths")
eq(dp.parse_recomphaven_consoles('window.RECOMPHAVEN_TABLES = {"consoles": {"n64": {"label": "Nintendo 64", "short": "N64", "color": "red;}"}}};'),
   {"n64": {"label": "Nintendo 64", "short": "N64", "color": ""}}, "console labels, colours checked")
# cached for a day; a failed refresh keeps the saved list
rh_tmp = Path(tempfile.mkdtemp(), "rh.json")
real_cache, real_get = dp.RH_CACHE, dp._rh_get
dp.RH_CACHE, calls = rh_tmp, []
dp._rh_get = lambda url: calls.append(url) or (RH_JS if url == dp.RH_DATA_URL else 'window.RECOMPHAVEN_TABLES = {"consoles": {}};')
eq(len(dp.recomphaven()["items"]), 2, "fetched"); n = len(calls)
eq((len(dp.recomphaven()["items"]), len(calls)), (2, n), "served from the cache")
def offline(url):
    raise OSError("offline")
dp._rh_get = offline
d = dp.recomphaven(refresh=True)
eq((len(d["items"]), "offline" in d["stale"]), (2, True), "stale cache when the refresh fails")
dp.RH_CACHE, dp._rh_get = real_cache, real_get

# --- Quiver catalog: apps joined with the repository each one releases from
QV_APPS = [
    {"slug": "old", "name": "Old Port", "projectName": "OldP", "consoles": ["n64"], "supportedOS": ["windows", "linux"],
     "aiLevel": "none", "addedAt": 1000, "lastReleaseAt": 5000, "verified": {"version": "v1"}, "recommended": 3,
     "libraryArt": {"capsule": "https://cdn2.steamgriddb.com/grid/a.png"}, "developer": {"name": "Dev"}},
    {"slug": "new", "name": "New Port", "consoles": ["ps1"], "supportedOS": ["linux"], "aiLevel": "weird",
     "addedAt": 2000, "verified": {"version": "v2"}, "artwork": "javascript:alert(1)"},
    {"slug": "gl", "name": "GitLab Port", "supportedOS": ["linux"], "addedAt": 1500, "verified": {"version": "v3"}},
    {"slug": "win", "name": "Windows only", "supportedOS": ["windows"], "verified": {"version": "v1"}},
    {"slug": "unverified", "name": "Unchecked", "supportedOS": ["linux"], "verified": None},
    {"slug": "norepo", "name": "No repo", "supportedOS": ["linux"], "verified": {"version": "v1"}},
]
QV_STATUS = [{"slug": "old", "provider": "github", "repository": "o/old"}, {"slug": "new", "provider": "github", "repository": "n/new"},
             {"slug": "gl", "provider": "gitlab", "repository": "grp/sub/gl"}, {"slug": "win", "provider": "github", "repository": "w/w"},
             {"slug": "unverified", "provider": "github", "repository": "u/u"}, {"slug": "norepo", "provider": "codeberg", "repository": "c/c"}]
qv = dp.parse_quiver(QV_APPS, QV_STATUS)
eq([x["id"] for x in qv], ["new", "gl", "old"], "verified Linux releases on GitHub or GitLab, newest first")
eq((qv[2]["repo"], qv[2]["key"], qv[2]["ai"], qv[2]["recommended"], qv[2]["released"], qv[2]["developer"]),
   ("o/old", "github.com/o/old", "none", 3, 5.0, "Dev"), "entry fields")
eq((qv[1]["provider"], qv[1]["key"]), ("gitlab", "gitlab.com/grp/sub/gl"), "GitLab subgroups")
eq((qv[2]["cover"], qv[0]["cover"], qv[0]["ai"]), ("https://cdn2.steamgriddb.com/thumb/a.jpg", "", ""), "SteamGridDB thumbnails, only https covers, known AI levels")
# paged lists follow nextCursor; cached for a day; a failed refresh keeps the saved list
real_cache, real_get = dp.QV_CACHE, dp._qv_get
dp.QV_CACHE, calls = Path(tempfile.mkdtemp(), "qv.json"), []
def qv_get(path):
    calls.append(path)
    if path.startswith("/apps"):
        return {"items": QV_APPS[:2], "nextCursor": "c/1", "isDone": False} if "cursor" not in path else {"items": QV_APPS[2:], "isDone": True}
    if path.startswith("/release-status"):
        return {"items": QV_STATUS, "isDone": True}
    return {"consoles": [{"id": "n64", "name": "Nintendo 64"}]}
dp._qv_get = qv_get
d = dp.quiver()
eq((len(d["items"]), d["consoles"], "cursor=c%2F1" in "".join(calls)), (3, {"n64": "Nintendo 64"}, True), "fetched every page")
n = len(calls)
eq((len(dp.quiver()["items"]), len(calls)), (3, n), "served from the cache")
dp._qv_get = offline
d = dp.quiver(refresh=True)
eq((len(d["items"]), "offline" in d["stale"]), (3, True), "stale cache when the refresh fails")
dp.QV_CACHE, dp._qv_get = real_cache, real_get

# --- Recomp Launcher catalog: Linux releases on GitHub, most stars first
RL_DATA = {"catalogVersion": 1, "platform": "pc", "apps": [
    {"id": "tiny", "name": "Tiny Port", "game": "Tiny Game", "origin": "x360", "repo": "p/tiny", "version": "2.0",
     "stars": 10, "downloads": 5, "author": "P", "systems": ["linux"], "cover": "javascript:alert(1)"},
    {"id": "win", "name": "Windows only", "repo": "w/w", "version": "1", "stars": 99999, "systems": ["windows"]},
    {"id": "soon", "name": "Not yet", "repo": "s/s", "version": "1", "stars": 50, "systems": ["linux"], "soon": True},
    {"id": "bad", "name": "Bad repo", "repo": "https://github.com/b/b", "version": "1", "stars": 60, "systems": ["linux"]},
    {"id": "zelda64recomp", "name": "Zelda 64: Recompiled", "game": "The Legend of Zelda: Majora's Mask", "origin": "n64",
     "repo": "Zelda64Recomp/Zelda64Recomp", "version": "1.2.2", "released": "2025-08-27", "added": "2026-10-26",
     "downloads": 378566, "stars": 7538, "author": "Zelda64Recomp", "blurb": "Majora's Mask as a PC game", "needs": "Your copy",
     "cover": "https://recompstore.com/media/pc-zelda64recomp-cover.jpg", "systems": ["windows", "linux", "macos"]},
], "origins": [{"id": "n64", "name": "Nintendo 64"}, {"id": "x360", "name": "Xbox 360"}]}
rl = dp.parse_recomplauncher(RL_DATA)
eq([x["id"] for x in rl], ["zelda64recomp", "tiny"], "Linux releases with a valid repo, most stars first")
eq((rl[0]["title"], rl[0]["game"], rl[0]["console"], rl[0]["repo"], rl[0]["key"]),
   ("Zelda 64: Recompiled", "The Legend of Zelda: Majora's Mask", "n64", "Zelda64Recomp/Zelda64Recomp", "github.com/zelda64recomp/zelda64recomp"), "entry names")
eq((rl[0]["version"], rl[0]["released"], rl[0]["added"], rl[0]["downloads"], rl[0]["stars"], rl[0]["developer"]),
   ("1.2.2", "2025-08-27", "2026-10-26", 378566, 7538, "Zelda64Recomp"), "entry numbers and developer")
eq((rl[0]["cover"], rl[1]["cover"]), ("https://recompstore.com/media/pc-zelda64recomp-cover.jpg", ""), "only https covers")
# fetched once a day; a failed refresh keeps the saved list; no list and no network is an error
real_cache, real_get = dp.RL_CACHE, dp._rl_get
dp.RL_CACHE, calls = Path(tempfile.mkdtemp(), "rl.json"), []
dp._rl_get = lambda: calls.append(1) or RL_DATA
d = dp.recomplauncher()
eq((len(d["items"]), d["consoles"]), (2, {"n64": "Nintendo 64", "x360": "Xbox 360"}), "fetched, consoles from origins")
n = len(calls)
eq((len(dp.recomplauncher()["items"]), len(calls)), (2, n), "served from the cache")
def rl_offline():
    raise OSError("offline")
dp._rl_get = rl_offline
d = dp.recomplauncher(refresh=True)
eq((len(d["items"]), "offline" in d["stale"]), (2, True), "stale cache when the refresh fails")
dp.RL_CACHE = Path(tempfile.mkdtemp(), "none.json")
try:
    dp.recomplauncher(); raise AssertionError("no cache and offline")
except RuntimeError:
    ok += 1
dp.RL_CACHE, dp._rl_get = real_cache, real_get

# Quiver items get the stars that Recomp Haven or Recomp Launcher already saved; nothing is fetched
real_rh, real_rl, real_qv = dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE
star_tmp = Path(tempfile.mkdtemp())
dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE = star_tmp / "rh.json", star_tmp / "rl.json", star_tmp / "qv.json"
dp.RH_CACHE.write_text(json.dumps({"items": [{"key": "github.com/o/old", "stars": 40}, {"key": "github.com/n/new", "stars": 3}]}))
dp.RL_CACHE.write_text("not json")
eq(dp._known_stars(), {"github.com/o/old": 40, "github.com/n/new": 3}, "a broken list is ignored")
dp.RL_CACHE.write_text(json.dumps({"items": [{"key": "github.com/o/old", "stars": 55}, {"key": "github.com/x/none", "stars": 1}]}))
eq(dp._known_stars(), {"github.com/o/old": 55, "github.com/n/new": 3, "github.com/x/none": 1}, "the higher count wins")
dp.QV_CACHE.write_text(json.dumps({"fetched_at": time.time(), "consoles": {}, "items": [
    {"key": "github.com/o/old", "title": "Old"}, {"key": "gitlab.com/grp/gl", "title": "GL"}]}))
qs = [(x["title"], x["stars"]) for x in dp.quiver()["items"]]
eq(qs, [("Old", 55), ("GL", 0)], "Quiver items carry the known stars, 0 when none")
eq("stars" in json.loads(dp.QV_CACHE.read_text())["items"][0], False, "stars not written to the Quiver cache")
dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE = real_rh, real_rl, real_qv

# --- Browse catalog: four sources parsed from small fixtures, merged by repository
BR_UB = {"games": [
    {"slug": "mindustry", "title": "Mindustry", "platform": "PC", "native_build": True, "runs_on": ["Windows", "Linux"],
     "url": "https://unbaked.games/games/mindustry/"},
    {"slug": "zelda-majoras-mask", "title": "The Legend of Zelda: Majora's Mask", "platform": "PC", "native_build": True,
     "runs_on": ["Linux"], "url": "https://unbaked.games/games/zelda-majoras-mask/"},
    {"slug": "win-only", "title": "Win Only", "native_build": True, "runs_on": ["Windows"], "url": "https://unbaked.games/games/win-only/"},
    {"slug": "not-native", "title": "Not Native", "native_build": False, "runs_on": ["Linux"], "url": "https://unbaked.games/games/not-native/"},
    {"slug": "Bad Slug", "title": "Bad", "native_build": True, "runs_on": ["Linux"]}]}
BR_DB = {"plans": {
    "github:HarbourMasters/2ship2harkinian": {"targets": {"linux": {"games": [
        {"game_title": "The Legend of Zelda: Majora's Mask", "console": "Nintendo 64", "build": {"method": "release"}}]}}},
    "gitlab:sonicdcer/dnzhrecomp": {"targets": {"linux": {"games": [
        {"game_title": "Donic Zelda", "console": "Dreamcast", "build": {"method": "release"}}]}}},
    "github:a/source-only": {"targets": {"linux": {"games": [
        {"game_title": "Source Only", "console": "PC", "build": {"method": "source"}}]}}},
    "github:a/windows-release": {"targets": {"windows": {"games": [
        {"game_title": "Win", "console": "PC", "build": {"method": "release"}}]}}},
    "github:a/bad": "junk"}}
BR_FG = """<table><thead><tr role="row"><th>Game</th></tr></thead><tbody>
<tr role="row" data-cat="recomp|Nintendo 64|Zelda|run:deck|ai:none" data-text="zelda">
<td role="cell" class="name"><a href="/games/zelda-64-recompiled.html">Zelda 64: Recompiled</a><span class="tag">recomp</span></td>
<td role="cell" data-label="Status" data-v="1"><span class="st st-fully-playable"><svg viewBox="0 0 12 12"><path d="M6 1"/></svg>Fully playable</span></td>
<td role="cell" data-label="Link" class="ext"><a href="https://github.com/Zelda64Recomp/Zelda64Recomp" rel="noopener">github.com</a></td>
<td role="cell" data-label="Console" data-v="Nintendo 64">Nintendo 64</td></tr>
<tr role="row" data-cat="fan|PC|Itchy|ai:none" data-text="itchy">
<td role="cell" class="name"><a href="/games/itchy.html">Itchy</a></td>
<td role="cell" data-label="Link" class="ext"><a href="https://itch.io/itchy">itch.io</a></td>
<td role="cell" data-label="Console" data-v="PC">PC</td></tr>
<tr role="row" data-cat="decomp|PC|Tool|ai:none" data-text="zed">
<td role="cell" class="name"><a href="/games/zed.html">Zed &amp; Co</a></td>
<td role="cell" data-label="Link" class="ext"><a href="https://archive.org/details/zed">archive</a><a href="https://gitlab.com/grp/zed/-/releases">gitlab</a></td>
<td role="cell" data-label="Console" data-v="PC">PC</td></tr>
</tbody></table>"""
BR_GE = """<div class="game-card" data-favourite-id="zelda-recomp" data-title="Zelda &#x27;Recomp&#x27;" data-category-display="In The Works" data-platform="N64">
<a class="game-card__link" href="https://github.com/Zelda64Recomp/Zelda64Recomp" target="_blank"><h3>x</h3></a></div>
<div class="game-card" data-favourite-id="adv" data-title="Advance Wars" data-category-display="Rom Hacks" data-platform="">
<a class="game-card__link" href="https://www.romhacking.net/hacks/6012/" target="_blank"></a></div>"""
ub_items = dp.parse_unbaked(BR_UB)
eq([x["id"] for x in ub_items], ["mindustry", "zelda-majoras-mask"], "unbaked: native Linux games with a valid slug")
eq((ub_items[0]["repo"], ub_items[0]["key"], ub_items[0]["url"], ub_items[0]["console"]),
   ("", "", "https://unbaked.games/games/mindustry/", "PC"), "unbaked entries have no repository")
db_items = dp.parse_decompbuddy(BR_DB)
eq([(x["id"], x["key"], x["provider"], x["title"], x["console"]) for x in db_items],
   [("github:HarbourMasters/2ship2harkinian", "github.com/harbourmasters/2ship2harkinian", "github",
     "The Legend of Zelda: Majora's Mask", "Nintendo 64"),
    ("gitlab:sonicdcer/dnzhrecomp", "gitlab.com/sonicdcer/dnzhrecomp", "gitlab", "Donic Zelda", "Dreamcast")],
   "decompbuddy: release on Linux only, gitlab keys")
fg_items = dp.parse_fangamesdb(BR_FG)
eq([(x["title"], x["key"], x["deck"], x["status"], x["console"], x["url"]) for x in fg_items],
   [("Zelda 64: Recompiled", "github.com/zelda64recomp/zelda64recomp", True, "Fully playable", "Nintendo 64",
     "https://fangamesdb.com/games/zelda-64-recompiled.html"),
    ("Zed & Co", "gitlab.com/grp/zed", False, "", "PC", "https://fangamesdb.com/games/zed.html")],
   "fangamesdb: repo links only, run:deck flag, entities")
ge_items = dp.parse_emporium(BR_GE)
eq([(x["title"], x["key"], x["console"], x["status"]) for x in ge_items],
   [("Zelda 'Recomp'", "github.com/zelda64recomp/zelda64recomp", "N64", "In The Works")],
   "emporium: repo links only, entities unescaped")
real_rh, real_rl, real_qv, real_brc = dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE, dp.BR_CACHE
real_br_json, real_br_html, real_cat_fetch = dp.http_json, dp._br_html, dp._catalog_fetch
dp._catalog_fetch = lambda src, refresh: ([], "")  # the three catalogs have their own tests; keep these offline
def set_br(fetch):
    """Serve Browse's network reads from fetch(url) -> text; OSError becomes the RuntimeError http_json raises."""
    def text(url, *a, **k):
        try:
            return fetch(url)
        except OSError as e:
            raise RuntimeError("Network error: %s" % e)
    dp._br_html = text
    dp.http_json = lambda url, *a, **k: json.loads(text(url))
br_tmp = Path(tempfile.mkdtemp())
dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE = br_tmp / "rh.json", br_tmp / "rl.json", br_tmp / "qv.json"
dp.RH_CACHE.write_text(json.dumps({"items": [{"key": "github.com/zelda64recomp/zelda64recomp", "stars": 7538}]}))
dp.RL_CACHE.write_text(json.dumps({"items": [{"key": "github.com/harbourmasters/2ship2harkinian", "stars": 3}]}))
dp.QV_CACHE.write_text(json.dumps({"items": [{"key": "github.com/zelda64recomp/zelda64recomp"}]}))
merged = dp.merge_browse({"ub": ub_items, "db": db_items, "fg": fg_items, "ge": ge_items})
eq([g["title"] for g in merged], ["Zelda 64: Recompiled", "The Legend of Zelda: Majora's Mask", "Donic Zelda", "Mindustry", "Zed & Co"],
   "most sources, then stars, then name; fg title beats emporium's")
zelda = merged[0]
eq((zelda["key"], zelda["host"], zelda["repo"], zelda["provider"], zelda["stars"], zelda["deck"]),
   ("github.com/zelda64recomp/zelda64recomp", "github.com", "Zelda64Recomp/Zelda64Recomp", "github", 7538, True),
   "merged by key, with stars")
eq([(s["src"], s["url"]) for s in zelda["sources"]],
   [("fg", "https://fangamesdb.com/games/zelda-64-recompiled.html"), ("ge", "https://thegamingemporium.com/all/")],
   "one card per repository, a source per site, best source first")
majora = merged[1]
eq((majora["key"], majora["stars"], [s["src"] for s in majora["sources"]]),
   ("github.com/harbourmasters/2ship2harkinian", 3, ["db", "ub"]), "unbaked title merges into the repository's card")
mind = merged[3]
eq((mind["repo"], mind["key"], mind["ub_slug"], mind["stars"], mind["sources"][0]["id"]),
   ("", "", "mindustry", 0, "mindustry"), "unbaked game without a repository stays standalone")
rh_item = {"key": "github.com/zelda64recomp/zelda64recomp", "host": "github.com", "repo": "Zelda64Recomp/Zelda64Recomp",
           "provider": "github", "title": "The Legend of Zelda: Majora's Mask", "console": "Nintendo 64", "deck": True,
           "status": "Released", "id": "mm", "url": "https://recomphaven.com/#mm", "cover": "https://x/c.webp", "summary": "Recomp"}
merged2 = dp.merge_browse({"rh": [rh_item], "fg": fg_items, "ge": ge_items})
z2 = next(g for g in merged2 if g["key"] == rh_item["key"])
eq(([s_["src"] for s_ in z2["sources"]], z2["title"], z2["cover"], z2["summary"]),
   (["rh", "fg", "ge"], "The Legend of Zelda: Majora's Mask", "https://x/c.webp", "Recomp"),
   "a catalog is a Browse source: its title, cover and summary win")
eq(len([g for g in merged if g["key"] == zelda["key"]]), 1, "same repository in two sources is one entry")
dp.RH_CACHE, dp.RL_CACHE, dp.QV_CACHE = real_rh, real_rl, real_qv

# browse(): a saved list per source, refetched once a day; a failing site keeps its list and the others update
FAKE = {dp.BR_SOURCES["ub"]["data"]: json.dumps(BR_UB), dp.BR_SOURCES["db"]["data"]: json.dumps(BR_DB),
        dp.BR_SOURCES["fg"]["data"]: BR_FG, dp.BR_SOURCES["ge"]["data"]: BR_GE}
br_calls = []
def br_get(url):
    br_calls.append(url)
    if FAKE.get(url) is None:
        raise OSError("offline")
    return FAKE[url]
set_br(br_get)
dp.BR_CACHE = Path(tempfile.mkdtemp(), "browse.json")
d = dp.browse()
eq(({s: v["count"] for s, v in d["sources"].items() if s in dp.BR_SOURCES}, d["stale"], len(d["items"])),
   ({"ub": 2, "db": 2, "fg": 2, "ge": 1}, False, 5), "every source read, merged")
eq(d["sources"]["fg"]["name"], "FanGamesDB", "source names for the page")
n = len(br_calls)
eq((len(dp.browse()["items"]), len(br_calls)), (5, n), "served from the saved lists")
FAKE[dp.BR_SOURCES["db"]["data"]] = None
d = dp.browse(refresh=True)
eq((d["sources"]["db"]["count"], "offline" in d["sources"]["db"]["error"], d["sources"]["ub"]["error"], d["stale"]),
   (2, True, "", True), "a failing site keeps its saved list and reports the error")
eq((len(d["items"]), dp.BR_SOURCES["ub"]["data"] in br_calls[n:]), (5, True), "the other sites still refreshed")
dp.BR_CACHE = Path(tempfile.mkdtemp(), "none.json")
for src in FAKE:
    FAKE[src] = None
try:
    dp.browse(); raise AssertionError("nothing saved and every site down")
except RuntimeError as e:
    eq("offline" in str(e), True, "error when no site has anything")
FAKE[dp.BR_SOURCES["ub"]["data"]] = json.dumps(BR_UB)
eq(dp.browse()["sources"]["ub"]["count"], 2, "a site that comes back is used again")
# a page that changed and parses to nothing is an error; the saved list stays
FAKE[dp.BR_SOURCES["fg"]["data"]] = BR_FG
eq(dp.browse(refresh=True)["sources"]["fg"]["count"], 2, "fangamesdb read")
FAKE[dp.BR_SOURCES["fg"]["data"]] = "<html>new layout</html>"
d = dp.browse(refresh=True)
eq((d["sources"]["fg"]["count"], "no projects" in d["sources"]["fg"]["error"]), (2, True), "empty parse keeps the saved list")
# a cache written by another version of the parsers is read again
cache = json.loads(dp.BR_CACHE.read_text())
eq(cache.get("format"), dp.BR_FORMAT, "cache records its format")
dp.BR_CACHE.write_text(json.dumps(dict(cache, format=dp.BR_FORMAT - 1)))
n = len(br_calls)
dp.browse()
eq(len(br_calls) > n, True, "old-format cache refetched")
real_browse = dp._browse
dp._browse = lambda refresh: dp._br_lock.locked()
eq(dp.browse(), True, "browse runs one reader at a time")
dp._browse = real_browse
# two unbaked.games games with the same title stay two entries
twins = [dp._br_item("ub", "doom", "Doom", url="https://unbaked.games/games/doom/"),
         dp._br_item("ub", "doom-2016", "DOOM", url="https://unbaked.games/games/doom-2016/")]
eq(sorted(g["ub_slug"] for g in dp.merge_browse({"ub": twins})), ["doom", "doom-2016"], "same-title games not collapsed")

# browse_resolve(): the best Linux project on a known forge, never an archived or Windows-only one
UB_GAME = {"slug": "mindustry", "projects": [
    {"name": "Old", "url": "https://gitlab.com/a/old", "runs_on": ["Linux"], "stars": 900, "archived": True},
    {"name": "Win", "url": "https://github.com/a/win", "runs_on": ["Windows"], "stars": 800, "archived": False},
    {"name": "Mine", "url": "https://github.com/Owner/Proj", "runs_on": ["Linux"], "stars": 10, "archived": False},
    {"name": "Codeberg", "url": "https://codeberg.org/a/b", "runs_on": ["Linux"], "stars": 20, "archived": False},
    {"name": "Itch", "url": "https://itch.io/x", "runs_on": ["Linux"], "stars": 500, "archived": False}]}
res_calls = []
set_br(lambda url: res_calls.append(url) or json.dumps(UB_GAME))
eq(dp.browse_resolve("mindustry"), "https://codeberg.org/a/b", "best Linux project wins")
eq(res_calls, ["https://unbaked.games/api/games/mindustry.json"], "fetched the per-game record")
for bad in ("../etc", "Mindustry", "a/b", ""):
    try:
        dp.browse_resolve(bad); raise AssertionError("bad slug accepted: %r" % bad)
    except ValueError:
        ok += 1
set_br(lambda url: json.dumps({"projects": [UB_GAME["projects"][1]]}))
try:
    dp.browse_resolve("mindustry"); raise AssertionError("no Linux project")
except RuntimeError as e:
    eq("no Linux project" in str(e), True, "clear error without a Linux project")
set_br(offline)
try:
    dp.browse_resolve("mindustry"); raise AssertionError("offline resolve")
except RuntimeError:
    ok += 1
dp.RH_CACHE, dp.BR_CACHE, dp.http_json, dp._br_html = real_rh, real_brc, real_br_json, real_br_html
dp._catalog_fetch = real_cat_fetch

# /api/add keeps the Browse source and link, and refuses a link off the source's site
real_check = dp.check_app
dp.check_app = lambda app, token="": None
added = srv.handle("/api/add", {"repo": "https://github.com/a/brtest", "br_src": "fg",
                                "br_url": "https://fangamesdb.com/games/zelda-64-recompiled.html"})
stored = next(a for a in srv.handle("/api/state", {})["apps"] if a["id"] == added["id"])
eq((stored["br_src"], stored["br_url"]), ("fg", "https://fangamesdb.com/games/zelda-64-recompiled.html"), "browse fields stored")
for bad in ({"br_src": "fg", "br_url": "https://evil.example/x"}, {"br_src": "fg", "br_url": "http://fangamesdb.com/x"},
            {"br_src": "zz", "br_url": "https://fangamesdb.com/x"}, {"br_src": "ub", "br_url": "https://fangamesdb.com/x"},
            {"br_src": ["fg"], "br_url": "https://fangamesdb.com/x"}):
    try:
        srv.handle("/api/add", dict(bad, repo="https://github.com/a/off%s" % len(bad)))
        raise AssertionError("browse link accepted: %r" % (bad,))
    except ValueError:
        ok += 1
dp.check_app = real_check
dp.BR_CACHE = real_brc

# --- Flatpak bundles
eq(dp.pick_asset(A("Game-x86_64.flatpak", "Game-linux.tar.gz", "Game.flatpakref"))["name"], "Game-x86_64.flatpak", "bundle beats a tarball")
eq(dp.pick_asset(A("Game-x86_64.flatpak", "Game-x86_64.AppImage"))["name"], "Game-x86_64.AppImage", "AppImage still wins")
FILES["https://x/game.flatpak"] = b"flatpak\x00" + b"\x00" * 40 + b"app/org.example.Game/x86_64/stable\x00" + b"\x00" * 100
fp_tmp, fp_calls = tempfile.mkdtemp(), []
real_cmd, real_dir = dp.flatpak_cmd, dp.flatpak_dir
dp.flatpak_cmd = lambda *a, **k: fp_calls.append(a) or ""
dp.flatpak_dir = lambda app_id: os.makedirs(os.path.join(fp_tmp, app_id), exist_ok=True) or os.path.join(fp_tmp, app_id)
fpapp = {"id": "fp1", "name": "Flat Game", "repo": "me/flat", "latest": {"tag": "v1", "asset": {
    "id": 9, "name": "game.flatpak", "size": 1, "updated_at": "t", "download_url": "https://x/game.flatpak", "sha256": ""}}}
fp_path = dp.install_app(fpapp, {"install_dir": fp_tmp, "keep_backup": True})
eq((fp_path, fpapp["installed"]["kind"], fpapp["installed"]["flatpak_id"]),
   (os.path.join(fp_tmp, "org.example.Game"), "flatpak", "org.example.Game"), "bundle installed through flatpak")
eq((fp_calls[0][0], "--user" in fp_calls[0], "--bundle" in fp_calls[0]), ("install", True, True), "flatpak install --user --bundle")
eq([f for f in os.listdir(fp_tmp) if f.startswith(".sedotdeck-")], [], "bundle download cleaned up")
eq((dp.steam_target(fpapp)[2], dp.steam_args(fpapp)), (False, "run org.example.Game"), "Steam starts flatpak run")
sv3 = Path(tempfile.mkdtemp(), "shortcuts.vdf")
eq(dp.add_to_steam(fpapp, sv3), True, "flatpak added to Steam")
ent = dict((k_, v) for _t, k_, v in dp.vdf_load(sv3.read_bytes())[0][2][0][2])
eq(ent["LaunchOptions"], "run org.example.Game", "launch options carry the app id")
eq(dp.add_to_steam(fpapp, sv3), False, "same flatpak not added twice")
try:
    dp.rollback_app(fpapp); raise AssertionError("flatpak rollback")
except RuntimeError:
    ok += 1
dp.remove_app({"settings": {}, "apps": [fpapp]}, fpapp, delete_files=True)
eq((fp_calls[-1][0], fp_calls[-1][-1]), ("uninstall", "org.example.Game"), "remove uninstalls it")
dp.flatpak_cmd, dp.flatpak_dir = real_cmd, real_dir

# --- versions, SedotDeck's own updates, GitHub search
vk = dp.version_key
eq(vk("0.0.1-alpha") < vk("0.0.1-alpha.2") < vk("0.0.1-beta") < vk("0.0.1") < vk("v0.0.2-alpha") < vk("0.1"), True, "version order")
eq(vk("nightly"), None, "not a version")
real_self, real_json, real_ver = dp.SELF_CACHE, dp.http_json, dp.VERSION
dp.SELF_CACHE, dp.VERSION = Path(tempfile.mkdtemp(), "self.json"), "0.0.1-alpha"
dp.http_json = lambda url, token="", service="": [
    {"tag_name": "v9.0.0", "draft": True, "assets": []},
    {"tag_name": "v0.0.2-alpha", "html_url": "https://x/r", "body": "notes", "assets": []},
    {"tag_name": "v0.0.1-alpha", "assets": []}]
info = dp.self_update_check()
eq((info["latest"], info["newer"], info["notes"]), ("v0.0.2-alpha", True, "notes"), "newest release that isn't a draft")
def no_network(*a, **k):
    raise AssertionError("should come from the cache")
dp.http_json = no_network
eq(dp.self_update_check()["latest"], "v0.0.2-alpha", "cached for a while")
dp.VERSION = "0.0.2-alpha"
eq(dp.self_update_check()["newer"], False, "same version is not an update")
def fake_search(url, token="", service=""):
    assert "in%3Aname%2Cdescription" in url and "sort=" not in url, url
    return {"total_count": 2, "items": [
        {"full_name": "a/bloodborne-port", "name": "bloodborne-port", "description": "d", "stargazers_count": 1500,
         "html_url": "https://github.com/a/bloodborne-port"},
        {"full_name": "../evil", "name": "x"}]}
dp.http_json = fake_search
r = dp.github_search("  bloodborne ", tracked=["A/Bloodborne-Port"])
eq([(i["repo"], i["stars"], i["tracked"]) for i in r["items"]], [("a/bloodborne-port", 1500, True)], "search results, tracked marked")
try:
    dp.github_search("x"); raise AssertionError("one letter searched")
except ValueError:
    ok += 1
searched = []
dp.http_json = lambda url, token="", service="": searched.append(url) or {"items": []}
for s in ("best", "stars", "updated"):
    dp.github_search("bloodborne", sort=s)
eq([("sort=" in u, "sort=stars" in u, "sort=updated" in u, "order=desc" in u) for u in searched],
   [(False, False, False, False), (True, True, False, True), (True, False, True, True)], "sort: best match, stars, updated")
dp.SELF_CACHE, dp.http_json, dp.VERSION = real_self, real_json, real_ver

# --- Trash and running apps
tr = Path(tempfile.mkdtemp())
real_xdg = os.environ.get("XDG_DATA_HOME")
os.environ["XDG_DATA_HOME"] = str(tr / "share")
for n in ("Game", "Game"):
    (tr / n).mkdir(); (tr / n / "run.sh").write_text("x")
    dp.move_to_trash(str(tr / n))
files = sorted(os.listdir(tr / "share" / "Trash" / "files"))
eq(files, ["Game", "Game.2"], "same name trashed twice keeps both")
info = (tr / "share" / "Trash" / "info" / "Game.2.trashinfo").read_text()
eq(info.splitlines()[:2], ["[Trash Info]", "Path=" + str(tr / "Game")], "trashinfo points at the old place")
eq((tr / "share" / "Trash" / "files" / "Game.2" / "run.sh").read_text(), "x", "folder contents kept")
(tr / "Solo.AppImage").write_text("a"); (tr / "Solo.AppImage.old").write_text("b")
cfg_t = {"settings": {}, "apps": [{"id": "s", "name": "Solo", "installed": {"path": str(tr / "Solo.AppImage"), "kind": "appimage"}}]}
dp.remove_app(cfg_t, cfg_t["apps"][0], delete_files=True)
eq((cfg_t["apps"], (tr / "Solo.AppImage").exists(), sorted(os.listdir(tr / "share" / "Trash" / "files"))),
   ([], False, ["Game", "Game.2", "Solo.AppImage", "Solo.AppImage.old"]), "remove trashes the AppImage and its backup")

proc = tr / "proc"
port = tr / "Port"; port.mkdir()
def fakeproc(pid, exe="/usr/bin/x", cwd="/", args=(), env=()):
    d = proc / str(pid); d.mkdir(parents=True)
    os.symlink(exe, d / "exe"); os.symlink(cwd, d / "cwd")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args))
    (d / "environ").write_bytes(b"\0".join(e.encode() for e in env))
real_proc, dp.PROC = dp.PROC, str(proc)
fakeproc(10, args=["/usr/bin/bash"])
fakeproc(11, args=["wine", str(tr / "Portable") + "/a.exe"])
eq(dp.running_from(str(port)), [], "a similar folder name isn't a match")
fakeproc(12, cwd=str(port))
fakeproc(13, env=["HOME=/x", "APPIMAGE=" + str(tr / "Solo.AppImage")])
eq((dp.running_from(str(port)), dp.running_from(str(tr / "Solo.AppImage"))), ([12], [13]), "cwd and $APPIMAGE found")
papp = {"id": "p", "name": "Port", "installed": {"path": str(port), "kind": "archive"},
        "latest": {"asset": {"name": "p.zip"}}}
for what, fn in (("update", lambda: dp.install_app(papp, {"install_dir": str(tr)})),
                 ("remove", lambda: dp.remove_app({"apps": [papp]}, papp, delete_files=True))):
    try:
        fn(); raise AssertionError(what + " of a running port went ahead")
    except RuntimeError as e:
        eq(("is running" in str(e), port.exists()), (True, True), what + " refused while running")
dp.PROC = real_proc
if real_xdg is None:
    del os.environ["XDG_DATA_HOME"]
else:
    os.environ["XDG_DATA_HOME"] = real_xdg

# Browse: titles that name another target platform are left out
eq([x["title"] for x in dp.parse_emporium('<div class="game-card" data-favourite-id="a" data-title="Game for Android"><a class="game-card__link" href="https://github.com/a/b">'
    '<div class="game-card" data-favourite-id="c" data-title="Game Switch Port"><a class="game-card__link" href="https://github.com/c/d">'
    '<div class="game-card" data-favourite-id="e" data-title="Switchblade Recomp"><a class="game-card__link" href="https://github.com/e/f">')],
   ["Switchblade Recomp"], "browse skips Android and Switch ports")

# --- leftover download folders: old ones are swept, a fresh one (a job in the other UI) is kept
wd = tempfile.mkdtemp()
for name, age in ((".sedotdeck-old", 2 * dp.WORK_DIR_MAX_AGE), (".sedotdeck-new", 60), ("keep-old", 2 * dp.WORK_DIR_MAX_AGE)):
    os.makedirs(os.path.join(wd, name, "inner"))
    then = time.time() - age
    os.utime(os.path.join(wd, name), (then, then))
eq(dp.sweep_work_dirs(wd), 1, "one stale work dir swept")
eq(sorted(os.listdir(wd)), [".sedotdeck-new", "keep-old"], "fresh work dir and other folders kept")
eq(dp.sweep_work_dirs(os.path.join(wd, "missing")), 0, "missing folder is fine")

# --- self-update installs only a file whose SHA-256 the release published
real = (dp.SELF_CACHE, dp.self_install_method, dp.download, dp.__file__)
sdir = tempfile.mkdtemp()
dp.SELF_CACHE = Path(sdir, "self.json")
script = os.path.join(sdir, "sedotdeck.py")
Path(script).write_text("old")
dp.__file__ = script
dp.self_install_method = lambda: "script"
good = b"#!/usr/bin/env python3\nprint('new')\n"
def fake_dl(url, dest, progress=None):
    Path(dest).write_bytes(good)
    return hashlib.sha256(good).hexdigest()
dp.download = fake_dl
def self_cache(assets):
    dp.SELF_CACHE.write_text(json.dumps({"fetched_at": time.time(), "release": {"tag_name": "v99.0.0", "assets": assets}}))
for assets, why in (([], "no asset"), ([{"name": "sedotdeck.py", "browser_download_url": "https://x/s", "sha256": ""}], "no digest"),
                    ([{"name": "sedotdeck.py", "browser_download_url": "https://x/s", "sha256": "0" * 64}], "wrong digest")):
    self_cache(assets)
    try:
        dp.self_update(); raise AssertionError("self-update should refuse: " + why)
    except RuntimeError:
        eq(Path(script).read_text(), "old", "self-update refused, script untouched: " + why)
self_cache([{"name": "sedotdeck.py", "browser_download_url": "https://x/s", "sha256": hashlib.sha256(good).hexdigest()}])
eq(dp.self_update(), "v99.0.0", "self-update with matching digest")
eq(Path(script).read_bytes(), good, "script replaced")
dp.SELF_CACHE, dp.self_install_method, dp.download, dp.__file__ = real

# --- theme setting: dark unless chosen, only known values stored
eq(srv.handle("/api/state", {})["settings"]["theme"], "dark", "theme defaults to dark")
srv.handle("/api/settings", {"theme": "light"})
eq(srv.handle("/api/state", {})["settings"]["theme"], "light", "theme saved")
srv.handle("/api/settings", {"theme": "neon"})
eq(srv.handle("/api/state", {})["settings"]["theme"], "light", "unknown theme ignored")
eq('data-theme-pref="__THEME__"' in dp.INDEX_HTML, True, "page carries the theme placeholder the server fills in")

# --- uninstall: files to the Trash, the app stays, Install puts it back under the same name
udir, utrash = tempfile.mkdtemp(), tempfile.mkdtemp()
real_xdg_u = os.environ.get("XDG_DATA_HOME")
os.environ["XDG_DATA_HOME"] = utrash
uapp = mkapp()
upath = dp.install_app(uapp, dict(settings, install_dir=udir))
dp.rename_app_file(uapp, Path(tempfile.mkdtemp(), "none.vdf"), name="Mine")
upath = uapp["installed"]["path"]
dp.uninstall_app(uapp)
eq((os.path.exists(upath), uapp.get("installed"), uapp["target_path"]), (False, None, upath), "uninstalled, path still pinned")
eq(len(os.listdir(os.path.join(utrash, "Trash", "files"))) >= 1, True, "file went to the Trash")
try:
    dp.uninstall_app(uapp); raise AssertionError("second uninstall should refuse")
except RuntimeError:
    ok += 1
eq(dp.install_app(uapp, dict(settings, install_dir=udir)), upath, "reinstall lands at the same file name")
# a folder port comes back to the same folder even if the app was renamed in between
uf = mkfolder("https://x/fold1", "v1")
ufdir = dp.install_app(uf, dict(settings, install_dir=udir))
dp.uninstall_app(uf)
uf["name"] = "Renamed Fold"
eq((os.path.exists(ufdir), dp.install_app(uf, dict(settings, install_dir=udir)), uf.get("folder_path")), (False, ufdir, None),
   "folder port reinstalls into its old folder")
if real_xdg_u is None:
    os.environ.pop("XDG_DATA_HOME", None)
else:
    os.environ["XDG_DATA_HOME"] = real_xdg_u

# --- two readers of a catalog at once (Browse and its own view): one fetch, no clash over the .tmp file
import threading as _th
real_rhc, real_get, real_parse = dp.RH_CACHE, dp._rh_get, dp.parse_recomphaven
dp.RH_CACHE = Path(tempfile.mkdtemp(), "rh.json")
rh_fetches, rh_errs = [], []
def slow_get(url):
    rh_fetches.append(url); time.sleep(0.05); return "[]"
dp._rh_get, dp.parse_recomphaven = slow_get, lambda text: [{"id": "a", "key": "github.com/a/b"}]
def read_rh():
    try:
        dp.recomphaven(False)
    except Exception as e:
        rh_errs.append(repr(e))
ths = [_th.Thread(target=read_rh) for _ in range(4)]
[t_.start() for t_ in ths]; [t_.join() for t_ in ths]
eq((rh_fetches.count(dp.RH_DATA_URL), rh_errs), (1, []), "concurrent catalog reads: one fetch, no errors")
dp.RH_CACHE, dp._rh_get, dp.parse_recomphaven = real_rhc, real_get, real_parse

# --- every version has its CHANGELOG section; the release workflow publishes that section as the notes
changelog = Path(os.path.dirname(os.path.abspath(sys.argv[1])), "CHANGELOG.md").read_text()
eq("\n## [%s] - " % dp.VERSION in changelog, True, "CHANGELOG.md has a section for %s" % dp.VERSION)
eq("\n## [Unreleased]\n" in changelog, True, "CHANGELOG.md keeps an Unreleased section for the next changes")

print("all %d checks passed" % ok)
