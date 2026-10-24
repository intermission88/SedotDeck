<div align="center">

<img src="assets/icon.svg" width="96" height="96" alt="SedotDeck icon">

# SedotDeck

**One place to keep every AppImage, Flatpak bundle and PC port on your Steam Deck up to date.**<br>
Paste a release link, search GitHub by a game's name, or pick a recomp from [Recomp Haven](https://recomphaven.com/). SedotDeck checks, downloads, verifies and swaps the old version out without breaking your Steam shortcuts.

![Steam Deck](https://img.shields.io/badge/Steam%20Deck-ready-1a9fff?style=flat-square&logo=steamdeck&logoColor=white)
![SteamOS](https://img.shields.io/badge/SteamOS-no%20sudo-1a9fff?style=flat-square&logo=steam&logoColor=white)
![Sources](https://img.shields.io/badge/GitHub%20·%20GitLab%20·%20Codeberg%20·%20Gitea%20·%20Forgejo-23272e?style=flat-square)

[Install](#install) · [Using it](#using-it) · [Discover](#discover-search-github) · [Recomp Haven](#recomp-haven) · [Folder ports](#folder-ports) · [FAQ](#faq) · [Credit](#credit)

</div>

> SedotDeck is in alpha (0.0.x). It was called DeckPort before; your app list moves over by itself on first start.

## Features

- **Update all** in one click, for apps released on **GitHub, GitLab, Codeberg, Gitea and Forgejo**.
- **Picks the right Linux file by itself**: an AppImage first, then a `.flatpak` bundle, then a `linux`/`steamdeck` build in a zip or tar. Windows, macOS and ARM builds are skipped.
- **Discover**: search GitHub for a game or port by name and see each project's stars before you add it.
- **Recomp Haven tab**: hundreds of recomps and PC ports with a Linux release, filterable by tier and system.
- **File paths never change**, so Steam shortcuts keep working after updates. **Add to Steam** works for AppImages, Flatpaks, folder ports and Windows `.exe` games through Proton.
- **AppImages you already have** in `~/Applications` are recognized and updated in place.
- **Safe**: SHA-256 verification, a `.old` backup with **Rollback**, and saves and configs in port folders are never touched.
- **Release control**: skip nightly releases, follow only the release marked **Latest**, **Skip** one version, and read **What's new** first.
- **Categories** and a **Select** mode for handling many apps at once.
- **Updates itself**: when a new SedotDeck release is out, it asks on start whether to update.
- **Decky plugin** for Game Mode, checking every 6 hours.
- **Light**: one Python file that runs on SteamOS's own Python. No `pip`, no `sudo`.

## Install

All three share the same app list and `~/Applications` folder, so you can install more than one. Downloads are on the [Releases](https://github.com/intermission88/SedotDeck/releases) page.

| Form | Runs in | Best for |
|---|---|---|
| **AppImage** `SedotDeck-x.y.z-x86_64.AppImage` | Desktop Mode | Its own window, no browser. **Recommended.** |
| **Decky plugin** `SedotDeck-decky-x.y.z.zip` | Game Mode | Updating without leaving Game Mode |
| **Script** `install.sh` | Desktop Mode | Smallest (about 100 KB), opens in a browser |

<details open>
<summary><b>AppImage (Desktop Mode)</b></summary>

1. Download `SedotDeck-…-x86_64.AppImage`.
2. Right-click it → **Properties** → **Permissions**, tick **Is executable**.
3. Double-click it.

It's about 80 MB because WebKitGTK is bundled; the only thing it needs from the system is `python3`, which SteamOS ships. The AppImage also works as a [CLI](#cli), and it can replace itself when you accept an update.
</details>

<details>
<summary><b>Decky plugin (Game Mode)</b></summary>

Needs [Decky Loader](https://decky.xyz).

1. Download `SedotDeck-decky-….zip` to the Deck. **Don't unzip it.**
2. **⋯** → **Decky** → **Settings** → **Developer**, turn on **Developer mode**.
3. **Install Plugin from ZIP File** and pick the ZIP.

Asset patterns and the GitHub token are set in the desktop version and the plugin uses them too. If you had the DeckPort plugin, uninstall it from Decky after installing this one.
</details>

<details>
<summary><b>Script (install.sh)</b></summary>

Copy this repository to the Deck (for example to `~/sedotdeck`), then in **Konsole**:

```bash
bash ~/sedotdeck/install.sh
```

Open **SedotDeck** from the app menu (*Utilities*). The script also removes an older DeckPort install.
</details>

## Using it

1. Press **Add app** and paste the repository link (`owner/repo` works too), search for it under **Discover**, or press **Add** in the **Recomp Haven** tab.
2. The preview shows which file will be downloaded. Wrong one? Open **Advanced** and pick the right file.
3. **Install**. From then on, **Update all** is enough.
4. **⋯** → **Add to Steam**, once (in Desktop Mode, close Steam first; see the [FAQ](#faq)).

Each row shows the app's status, its version, **Install/Update** and **Launch**. Everything else (Check, Skip, Rollback, Add to Steam, Choose program, Category, Edit, Remove) is in the **⋯** menu.

### Categories and Select mode

Give an app a category with **⋯** → **Category…** and the list is grouped by it. Press **Select**, pick a few apps, then **Check**, **Update**, **Category…** or **Remove…** them together.

### Choosing releases

In **⋯** → **Edit** → **Advanced**. The preview updates as you type.

| Option | What it does |
|---|---|
| **Skip releases named** | Ignores releases whose title or tag match a regex, for example `nightly\|beta`. |
| **Only the release marked "Latest"** | Follows only the release the developer labelled Latest. |
| **Asset pattern** | Picks a specific file; click a file in the list and the pattern is filled in for you. |
| **Include pre-releases** | Also follows pre-releases. |

## Discover: search GitHub

The **Discover** tab searches GitHub repositories by name and description, for example `bloodborne` or `god of war 3`. Each result shows its stars, language and when it was last updated, and you can sort by **Best match** or **Most stars**. **Add** opens the usual dialog, which looks at the project's latest release and shows the file it would download before anything is saved.

Without a GitHub token, GitHub allows 10 searches a minute.

## Recomp Haven

[Recomp Haven](https://recomphaven.com/) tracks every game recompilation and PC port. SedotDeck's **Recomp Haven** tab lists the ones with a Linux release on GitHub, using the site's own logo, box art and tiers.

- Search by game or project, filter by **system** (console) or tier, or show only the ones marked **Steam Deck**.
- Sort by tier, system (grouped under a heading per console), stars, name or newest release.
- **Add** puts it in **My apps**; **Details ↗** opens its page on recomphaven.com.
- Projects that hand out game files (ROMs or ISOs) are not shown. Every recomp needs your own copy of the game.

## Folder ports

A port released as a `.zip` or `.tar.gz` is extracted to `~/Applications/<Name>/`. Updates overwrite the release's files and delete files the old release had and the new one doesn't, without touching saves or configs.

To start it, SedotDeck writes a small launcher, `sedotdeck-launch.sh`, **inside that folder**. It changes into the game's folder and runs the program, so the Steam shortcut points at a path that never changes, even when a release renames its binary (`Game-1.0.x86_64` to `Game-1.1.x86_64`). Updates leave the launcher alone because it isn't part of any release.

Older versions put the launcher next to the folder as `~/Applications/<Name>.sh`. On the next update or program change, SedotDeck moves it into the folder, unless a Steam shortcut still points at the old one; then it stays so the shortcut keeps working. Remove that shortcut and use **Add to Steam** again to switch.

**⋯** → **Choose program…** picks which program runs, and the choice is kept across updates. With OpenGOAL, for example, pick `gk` (the game), not `goalc` (the compiler).

Picking an **`.exe`** means a Windows game. The Decky plugin turns Proton on for you. In Desktop Mode, turn it on yourself in Steam: **Properties** → **Compatibility** → **Force Proton**.

## Flatpak bundles

When a release ships a `.flatpak` bundle and no AppImage, SedotDeck installs it with `flatpak install --user`, and updates reinstall the new bundle. Your app data in `~/.var/app` stays. **Launch** and **Add to Steam** start it with `flatpak run <app id>`. Removing it with "Also delete its downloaded files" uninstalls it. Flatpaks have no `.old` backup, so Rollback isn't available for them.

Apps you installed from Flathub (through the KDE Discover store, say) are updated by Flatpak itself and aren't shown here.

## Already have the AppImage?

SedotDeck scans `~/Applications` and recognizes AppImages from the update information embedded in them (Vita3K and PPSSPP, for example). Press **Keep … up to date**: new versions are written to the same path and file name, so your Steam shortcut keeps working. Files it doesn't recognize can be linked with **Choose repo…** or hidden with **Hide**.

## CLI

<details>
<summary><b>The <code>sedotdeck</code> command</b> (from <code>install.sh</code>) or <code>./SedotDeck-…AppImage</code></summary>

```bash
sedotdeck add https://github.com/owner/repo --name "My Port"
sedotdeck add <repo> --pattern 'linux.*\.AppImage$'   # pick the asset yourself (regex)
sedotdeck add <repo> --pre                            # also follow pre-releases
sedotdeck add <repo> --adopt ~/Applications/Game-v1.1.AppImage
sedotdeck list
sedotdeck scan [--track]         # find AppImages already in ~/Applications
sedotdeck check
sedotdeck update                 # update everything that has a new version
sedotdeck update "My Port"       # update one app
sedotdeck remove "My Port" --delete-files
sedotdeck export apps.json       # back up or share the app list
sedotdeck import apps.json       # merge; apps already tracked are skipped
```
</details>

## FAQ

<details>
<summary><b>"GitHub rate limit hit"</b></summary>

Without signing in, GitHub allows 60 requests an hour (10 searches a minute). Create a *fine-grained* token with read-only access to public repositories at <https://github.com/settings/tokens> and paste it in **Settings**. The token is never included in exports.
</details>

<details>
<summary><b>SedotDeck picked the wrong file or release</b></summary>

**⋯** → **Edit** → **Advanced**: click the right file, or fill in **Skip releases named** (for example `nightly|beta`). See [Choosing releases](#choosing-releases).
</details>

<details>
<summary><b>A folder port can't be added to Steam</b></summary>

SedotDeck doesn't know which program to start yet. Use **⋯** → **Choose program…** and pick its `.sh`, binary or `.exe`.
</details>

<details>
<summary><b>Why do I have to close Steam for "Add to Steam"?</b></summary>

Steam rewrites `shortcuts.vdf` from memory when it exits. In Desktop Mode, exit Steam first, press **Add to Steam**, then start Steam again. The previous file is always kept as `shortcuts.vdf.sedotdeck.bak`. In Game Mode there's no need: the Decky plugin uses Steam's own API.
</details>

<details>
<summary><b>The AppImage won't open</b></summary>

Make sure **Is executable** is ticked (or run `chmod +x SedotDeck-*.AppImage`). *"SedotDeck needs Python 3"* means your system has no `python3`; SteamOS includes it.
</details>

<details>
<summary><b>How do SedotDeck's own updates work?</b></summary>

On start, SedotDeck asks GitHub for its newest release (at most every 6 hours). If it's newer, you get **Update now**, **Later** or **Skip this version**. The AppImage downloads the new AppImage and replaces itself; the `install.sh` version replaces `sedotdeck.py`. Restart SedotDeck to use the new version. The Decky plugin can't replace itself, so it links to the release instead. You can check any time in **Settings**.
</details>

<details>
<summary><b>Where does SedotDeck keep its data?</b></summary>

- App list and settings: `~/.config/sedotdeck/config.json` (moved from `~/.config/deckport` if you used DeckPort)
- Recomp Haven cache (one day): `~/.config/sedotdeck/recomphaven.json`
- Games and apps: `~/Applications/` (change it in **Settings**)

SedotDeck sends no telemetry. It only contacts the forge APIs where releases live, GitHub's search when you use Discover, and Recomp Haven when its tab is open.
</details>

## How it works

<details>
<summary><b>Architecture and building it yourself</b></summary>

```mermaid
flowchart LR
  A["AppImage<br/>(Tauri window)"] -- "starts on a free localhost port" --> P["sedotdeck.py"]
  S["install.sh<br/>(browser)"] --> P
  D["Decky plugin<br/>(Game Mode)"] -- "imports" --> P
  P -- "release + search API" --> F["GitHub · GitLab · Codeberg<br/>Gitea · Forgejo"]
  P -- "download + SHA-256" --> L["~/Applications"]
  P -- "flatpak install --user" --> K["Flatpak"]
  P -- "Add to Steam" --> V["Steam shortcuts.vdf"]
  R["Recomp Haven"] -- "recomp list" --> P
  P <--> C["~/.config/sedotdeck"]
```

All the logic is in [`sedotdeck.py`](sedotdeck.py) (Python standard library only). [`desktop/`](desktop) is a Tauri shell that shows its UI in a native window, and [`decky-plugin/`](decky-plugin) calls the same code from Game Mode.

- **AppImage**: `cd desktop && npm ci && npm run build` (needs Rust, Node.js and the [Tauri dependencies](https://v2.tauri.app/start/prerequisites/#linux)).
- **Decky plugin**: `decky-plugin/package.sh` (needs Node.js 20+ and `zip`).
- **Tests**: `python3 -I tests/test_sedotdeck.py sedotdeck.py` (on Linux).

GitHub Actions builds both on every push to `main`, and `v*` tags are published as a Release.
</details>

## Credit

SedotDeck is built on other people's work:

| Feature | Credit |
|---|---|
| Recomp Haven tab (list, tiers, summaries, system labels, logo) | [Recomp Haven](https://recomphaven.com/) by [rikuwru](https://github.com/rikuwru/recomphaven) |
| Recomp Haven's base data | [Recompendium](https://github.com/nio03/unricopie) (MIT) |
| Box art in the Recomp Haven tab | Belongs to each game's publisher, collected by Recomp Haven from [libretro thumbnails](https://thumbnails.libretro.com/) and Wikipedia |
| The idea behind SedotDeck, release title filters, "Latest" releases, moved-repo detection, categories and bulk actions | [Obtainium](https://github.com/ImranR98/Obtainium) by ImranR98 |
| Recognizing AppImages from embedded update information | The update information spec from [AppImage](https://github.com/AppImage/AppImageSpec) |
| Desktop window (AppImage) | [Tauri](https://v2.tauri.app) and WebKitGTK |
| Game Mode plugin | [Decky Loader](https://decky.xyz) (`@decky/api`, `@decky/ui`) and [react-icons](https://react-icons.github.io/react-icons/) |
| Windows games through Proton | [Proton](https://github.com/ValveSoftware/Proton) by Valve |
| Code and documentation | [Claude](https://claude.com/claude-code) by Anthropic (see below) |

The emulators and ports you install with SedotDeck are their developers' work. Support them in their own repositories.

### Made with Claude

SedotDeck is built with [Claude](https://claude.com/claude-code), Anthropic's AI model, through Claude Code. The direction and decisions are mine ([intermission88](https://github.com/intermission88)), and I test every feature myself. Commits made with Claude carry `Co-Authored-By: Claude`.
