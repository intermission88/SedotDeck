# Changelog

What changed in each SedotDeck release, newest first. The section for a version is also the text of its [GitHub release](https://github.com/intermission88/SedotDeck/releases).

## [Unreleased]

## [0.1.0] - 2026-11-04

SedotDeck 0.1.0 leaves alpha: save backups before every update, uninstall, downloads you can cancel, and a redesign with themes and controller sounds.

**What's new**
- Save backups: a game's saves are zipped before every update (the newest 10 are kept), and if the backup fails the update doesn't run. Restore can be undone. Set it up under Saves… in the app menu
- Uninstall moves an app's files to the Trash but keeps it in My apps; Install puts it back and its Steam shortcut works again
- Downloads can be cancelled from the app card, the Decky panel or the update dialog
- New AppImages keep their project's name minus the version (`Savage_Wind-0.4.1-alpha.4-x86_64.AppImage` becomes `Savage_Wind-x86_64.AppImage`). Rename file… sets any name, and Settings > Auto rename renames after each update
- Theme setting: Dark (default), Light or Match system
- Controller sound effects, on by default while you use a controller (Settings > Sound effects)

**Redesign**
- Three tabs: My apps, Browse and Search GitHub. Browse shows one catalog at a time, in that site's own colors and style
- My apps shows box art, and suggests Deck-tested projects while your list is short
- Search GitHub suggests your recent searches and Deck-tested games
- A new typeface and short animations ("Reduce motion" turns them off)
- A shorter app menu, and Settings grouped into General, Updates and Advanced

**Decky**
- One button: Update all (N) or Check for updates
- "What's new" shows an update's release notes before you install it

**Under the hood**
- SedotDeck updates itself only when the download matches the release's SHA-256, and script installs download from the release
- The AppImage carries update information and each release has a `.zsync`, so AppImageUpdate works
- Download folders left behind by a crash are cleaned up on the next install

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

[Unreleased]: https://github.com/intermission88/SedotDeck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/intermission88/SedotDeck/releases/tag/v0.1.0
[0.0.4-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.4-alpha
[0.0.3-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.3-alpha
[0.0.2-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.2-alpha
[0.0.1-alpha]: https://github.com/intermission88/SedotDeck/releases/tag/v0.0.1-alpha
