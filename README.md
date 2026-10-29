# Initial D Street Stage (PSP) — Native PC Port

A native Windows port of *Initial D Street Stage* (PSP, ULJM05093). The PSP's MIPS `BOOT.BIN` is statically
recompiled to C and linked against a hand-written runtime that replaces the PSP OS/hardware. Your own game ISO
supplies all game data; nothing is redistributed.

> You must source your own copy of the Initial D Street Stage ISO. It will not be provided.

**Leaderboard:** [cassnadra.github.io/initialdpsponpc](https://cassnadra.github.io/initialdpsponpc/) - every course's time attack ranking, with search, filters and ghost replays on the course map (also in game: F1 -> Online -> Leaderboard -> *Open on the website*).

## Features

- High-resolution, high-frame-rate gameplay (smoothing / frame interpolation).
- Controller + keyboard remapping; gamepad hot-swap.
- LAN and Internet party races (up to 4 players).
- Online leaderboards with ghost racing (download and race other players' runs) - [ranking website](https://cassnadra.github.io/initialdpsponpc/).
- Modding layer: custom cars, courses, music, skins/texture packs, skyboxes, liveries.
- Multiple ghosts (up to 8 of your fastest runs race alongside you).
- Replay theatre with save states, free camera, keyframes, slow motion, MP4 export.
- Video recording (F5) to MP4.
- Replay sharing via `.idreplay` files (drag-and-drop / `replays\inbox`).
- NPC theatre (your car drives itself along your best line, 1-3 NPCs race it).
- Streamer mode, custom themes, rave mode, photo mode.

## Requirements

- Windows 10/11 (64-bit).
- Your own `InitialD_EN.iso` / JP ISO placed in the game folder.
- For online features: Steam (for room/quick match) — optional.
- GPU with D3D11 support.

## Quick start

1. Put the ISO in the game folder (next to `initiald.exe`).
2. Run `initiald_en.exe` (English) or `initiald.exe` (Japanese).
3. On first launch the game extracts/runs from the ISO. F1 opens the settings menu.

## Controls (defaults)

- Arrow keys / D-pad: navigate menus.
- Enter / Cross: confirm; Esc / Circle: back.
- F1: settings menu (8 tabs).
- F5: start/stop video recording.
- F6: hood camera toggle. F8: free camera.
- Shift+F5: export the replay currently playing to MP4.
- In a replay: `,` / `.` step frames, `[` / `]` slow motion, `K` keyframe, `L` play camera path,
  `F` follow car, `H` hide HUD, `P` pause.

## F1 menu tabs

Display, Lighting, World (rave/sky/themes/road), Car look (paint/wheels/underglow/trail), Race (ghosts/party/
cameras/FOV), Music, Audio, Controls, Car, Replays + video.

"Find a setting" box at the top jumps straight to a page.

## Ghosts

Every run you drive (>= 20s) is saved to `ghosts\<course>_<date>.idg`. Next time on the same course + start
(within 20m) your fastest complete runs race alongside you (My ghosts, 0-8, default 3). Opacity, trails, and
"race in the game's replays too" are configurable on the Race tab.

Leaderboard ghosts (other players' runs) are downloaded into `ghosts\online\` and race on that course too.

## Replays

Save a replay after a Time Attack run (Project D -> Replay). The F1 -> Replays + video tab lists saved slots
and lets you export them as `.idreplay` (in `replays\`) or queue them for MP4 export (open the replay in-game
and it exports automatically).

Import someone's replay: drag the `.idreplay` onto the game window, or drop it in `replays\inbox` (imported at
start, moved to `replays\imported`).

## Leaderboard

F1 -> Online -> Leaderboard: course combo, table, "Race this ghost" downloads a run into `ghosts\online\`.
Runs better than your best are submitted automatically (with the ghost attached). Admin builds add moderation
(Delete/Mute/Ban).

## Modding

See `MODDING.md` for the full tool reference. Highlights:

- Cars: drop a folder under `mods\` / liveries; skins in `liveries\carNN - Name\skin.png`.
- Music: `music\` folder; custom songs loop full length; shuffle modes.
- Texture packs / themes / skyboxes: `themes\`, `skyboxes\`.
- Car snapshots (looks) saved in `carsnap\` after you drive a car.

## Building (developers)

```
python tools/build.py                # JP build -> build\initiald.exe
python tools/build.py --game en      # EN build -> build\initiald_en.exe
python tools/build.py --admin        # admin build (moderation UI)
python tools/build.py --exe foo.exe  # custom output name (for queuing builds)
```

Uses ninja + llvm-mingw in `toolchain\`. Runtime compiled at `-O3 -msse4.2`.

`tools/make_dist.py` builds a distributable (keeps personal dirs like `savedata`, `ghosts`, `replays`).

## Troubleshooting

- Crashes: use the in-built crash reporter; send the minidump + `port.log`.
- Missing textures/cars: drive the car once so its look is saved (`carsnap\`).
- ISO not found: place it next to the exe and named as expected.
- Online not working: ensure Steam is running; check `settings.ini` (`lb_steam_id`).

## Project layout

- `runtime\` — C/C++ runtime (recompiled game + OS/hardware replacement).
- `tools\` — Python: recompiler, porting tools, leaderboard bot, build scripts.
- `build\` — generated C + all `.exe`s.
- `dist\repo` — the GitHub repository (`cassnadra/initialdpsponpc`).
- `docs\` — extra documentation (leaderboard pipeline, etc.).

## Credits / notes

Native port by the project maintainer. Game data remains the user's own. Not affiliated with Sega/Cavia.

For issues: Discord `@cassnadra`. Crashes have an in-built reporter — please use it.
