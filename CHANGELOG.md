# Changelog

What changed in each SedotDeck release, newest first. The section for a version is also the text of its [GitHub release](https://github.com/intermission88/SedotDeck/releases).

## [Unreleased]

- Fewer tabs: My apps, Browse and Search GitHub. Browse shows the seven catalogs one at a time, each picked from a row of buttons and drawn in its own site's colors, typefaces and card shape (light or dark where the site has both); Recomp Haven, Quiver and Recomp Launcher keep their own tiers and filters
- My apps shows each app's box art when a catalog lists its repository. With fewer than eight apps, Steam Deck-tested projects (the most starred among the latest releases, one per series) and a way into each catalog sit under the list; an empty My apps starts with a box to paste a release link
- Save backups: Saves… in the app menu (and `sedotdeck.py saves`) sets where a game keeps its saves, with suggestions named after the game. The folder is zipped before every update and on Back up now, the newest 10 kept in `~/Applications/Save backups`. Restore backs up the current saves first and moves them to the Trash, so it can be undone. If the backup fails, the update doesn't run
- A new typeface, Bricolage Grotesque, with bolder headings; without a connection SedotDeck falls back to the system font. Main buttons, the start panels and apps with an update get a soft blue gradient
- Sound effects for the controller: a tick per D-pad step, a knock at an edge, pips for A and B, a slide for LB/RB, and a short chord when an app is added, installed or its saves are backed up. Settings > Sound effects: With a controller (the default), Always or Off
- Short animations: a tab or catalog rises in when it opens, newly loaded cards come in one after another, dialogs and the app menu grow in, toasts rise from the bottom, and poster cards lift under the pointer or d-pad. "Reduce motion" in the system settings turns them off
- Search GitHub starts with your recent searches and the names of Deck-tested games from the catalogs (picked the same way), one click to search each; results show the owner's avatar and the repo's topics
- The app menu is shorter and grouped: Category moved into Edit, the two rename items are one, per-app Check for updates is gone (the header button and the hourly check cover it), and catalog pages are a single "View on …"
- Settings are grouped into General, Updates and Advanced (GitHub token, app list, storage); the Add dialog's options are grouped by which release and which file
- When adopting a file you already have, "Rename to" shows the real new name (the AppImage's own name without the version)
- Decky: one button that reads Update all (N) or Check for updates, and "What's new" moved into the app menu
- The AppImage carries update information and each release has a `.zsync` file, so AppImageUpdate and similar tools can update SedotDeck
- Settings has a Theme option: Dark (the default), Light, or Match system
- A new AppImage keeps the name its project gives it, minus what changes between releases: the version, a date, a build number or a commit hash (`Savage_Wind-0.4.1-alpha.4-x86_64.AppImage` becomes `Savage_Wind-x86_64.AppImage`), instead of being named after the app. Apps you already have keep their file
- Rename file… in the app menu sets any file name; updates and the Steam shortcut follow it. The one-click rename now suggests the AppImage's own name without the version
- Settings: Auto rename (off by default) gives an AppImage its own name without the version after each update, and "Rename N files now" does it for every app at once
- Decky: "What's new" shows an update's release notes before you install it, and the app menu shows the notes of the current release
- Uninstall (app menu, desktop, Decky and `sedotdeck.py uninstall`) moves an app's files to the Trash but keeps it in My apps; Install puts it back under the same file name, so its Steam shortcut works again
- Downloads can be cancelled: Cancel on the app card, Cancel download in the Decky panel, and in the SedotDeck update dialog. The installed version stays as it was and the partial file is deleted
- Download folders left behind by a crash or power-off are cleaned up on the next install
- SedotDeck's self-update installs only a download whose SHA-256 matches the one the release published; script installs now download `sedotdeck.py` from the release instead of the repository

## [0.0.4-alpha] - 2026-11-03

SedotDeck 0.0.4-alpha adds a Browse tab that merges four catalogs, support for repos without releases, and safer removes.

**What's new**
- Browse tab: ports, decomps and recomps from [unbaked.games](https://unbaked.games/), [DecompBuddy](https://decompbuddy.com/), [FanGamesDB](https://fangamesdb.com/) and [The Gaming Emporium](https://thegamingemporium.com/) in one list, merged by repository, with filters for source, system and "Tested on Steam Deck"
- Repos that publish no releases, only git tags, install from the newest tag's source code; rolling tags such as `nightly` count as pre-releases
- The Decky plugin can include pre-releases when adding an app
- When two files are otherwise equal, the PGO build is picked
- Removing an app moves its files to the Trash instead of deleting them
- Updating, or deleting the files of, an app that's running is refused
- App cards say what a running job is doing (Downloading…, Verifying…, Installing…) instead of "Checking…"
- A bug report form on GitHub

**Fixes**
- Decky: the side panel no longer gets wider during a download, and long tags or file names wrap

SedotDeck isn't part of any of the Browse sites; their lists are read from public data. DecompBuddy's catalog is GPL-3.0. recomphub.com and decomps.net aren't included because their robots.txt asks programs to stay out of their API.

## [0.0.3-alpha] - 2026-10-27

SedotDeck 0.0.3-alpha adds a Recomp Launcher tab, star counts, update progress and controller support.

**What's new**
- Recomp Launcher tab: PC recomps with a Linux release from the [recomplauncher.com](https://recomplauncher.com/) catalog, sortable by stars, newest release, newly added, system or name
- Catalog cards show GitHub stars; Quiver can sort by most stars
- Discover can sort by recently updated
- Updating SedotDeck shows a progress bar with size and speed, and keeps downloading while you use the app or after the window closes
- Controller support: D-pad or left stick to move, A to select, B to go back, X for the app menu, Y to search, LB/RB to switch tabs

SedotDeck isn't part of RecompLauncher; the list is read from its public catalog.

## [0.0.2-alpha] - 2026-10-26

SedotDeck 0.0.2-alpha adds a Quiver tab next to Recomp Haven.

**What's new**
- Quiver tab: ports and recomps with a verified Linux release from the [quiverlauncher.com](https://quiverlauncher.com/) catalog, with box art and an Add button
- Filter by system or by AI use (no AI, AI-assisted, AI-generated), and sort by newly added, newest release or most recommended
- Player reports from the catalog show on each card
- The list is kept for a day; Refresh fetches it again

SedotDeck isn't part of Quiver; the list is read from Quiver's public catalog.

## [0.0.1-alpha] - 2026-10-24

First alpha of SedotDeck, the app formerly called DeckPort.

**What's in it**
- Keeps AppImages, `.flatpak` bundles and PC ports up to date from GitHub, GitLab, Codeberg, Gitea and Forgejo
- Discover: search GitHub for a game or port by name, with stars
- Recomp Haven tab with tier and system filters
- Folder ports keep their launcher inside their own folder
- Asks to update itself when a new release is out
- A new look close to SteamOS, and a new logo

Coming from DeckPort? Your app list moves over on first start.

[Unreleased]: https://github.com/intermission88/SedotDeck/compare/v0.0.4-alpha...HEAD
[0.0.4-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.4-alpha
[0.0.3-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.3-alpha
[0.0.2-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.2-alpha
[0.0.1-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.1-alpha
