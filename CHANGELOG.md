# Changelog

What changed in each SedotDeck release, newest first. The section for a version is also the text of its [GitHub release](https://github.com/intermission88/SedotDeck/releases).

## [Unreleased]

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
