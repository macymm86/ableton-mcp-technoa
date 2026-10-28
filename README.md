# Ableton MCP – Technoa edition

An MCP server and Ableton Live Remote Script that let an AI assistant (Claude via
the Model Context Protocol) **see a whole Live set and work in it safely**: groups,
returns, master, locators, automation state, arrangement notes, routing, sidechains,
meters in dBFS, clip automation – plus an undo model you can trust and snapshots
that show exactly what changed.

It started as a fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp)
and was rebuilt step by step while producing techno with it, with one goal: the
assistant should be a real co-producer, not a read-only observer. It cannot hear –
listening stays with you.

> **Status:** a one-time public snapshot. It is developed further in a private repo
> and may be updated here by hand now and then, without promises. Issues and pull
> requests may go unanswered.

> **Language:** tool descriptions, tool replies and code comments are **German**.
> Language models handle that without trouble; this README is English.

## What is different from the original

| Original (PyPI 1.4.5 / Remote Script 1.7.1) | This edition |
|---|---|
| Groups show up as audio tracks | Track tree with groups, outputs (`-> DRUMS`), fold state |
| Deactivated clips not visible | `DEAKTIVIERT` per clip and counted per track |
| No locators, returns or master | Locators, returns (A, B, …) and master in the overview and addressable |
| Levels as raw 0..1 values | Live's own display texts: `-12.0 dB`, `25L`, `1/16` |
| Automation invisible | Automation state per parameter (active / overridden), curves from the saved `.als` |
| `get_clip_notes` for session clips only | Arrangement clips too, by index or "the clip at bar X" |
| 500 kick clips = 120,000 characters | Repeating clips summarised: "pattern of 4 clips, 125x" |
| Reads from the socket thread | Everything on Live's main thread; ~100 ms instead of 300–400 ms per command |
| One command = several undo steps | One command = one undo step; `undo`/`redo` only touch steps the MCP made itself |
| Overridden automation invisible | Warning in the overview, `re_enable_automation` |
| Telemetry and dataset code switched off | **Removed entirely**, as is the `user_prompt` field |

New on top: set snapshots and diffs, undo marks with a foreign-change check, clip
automation, groove per range, track routing and sidechain sources, device management
(insert, move, duplicate, delete with backup), dBFS meters with a reference track,
note fields like `velocity_deviation` and `release_velocity`, and guards against
overwriting arrangement material.

## Tools (59)

Tracks can be given everywhere as index, name (or a unique part of it), `A`/`B` for
returns, or `master`. Bars are 1-based, as in Live.

| Area | Tools |
|---|---|
| **Look** | `get_set_overview` (start here: the whole set on one page), `get_track_info`, `get_arrangement_clips`, `get_clip_notes`, `get_device_parameters`, `find_parameter_value`, `get_selection`, `get_session_info`, `get_remote_script_info`, `get_routing_options`, `get_clip_envelope`, `read_saved_set`, `inspect_lom`, `get_browser_tree`, `get_browser_items_at_path` |
| **Measure** | `watch_meters` (levels over time while playing, dBFS, L/R, distance to a reference track, master headroom, muted tracks flagged) |
| **Mixer & devices** | `set_mixer` (dB, pan, sends, mute/solo/arm), `set_device_parameter`, `insert_device`, `load_instrument_or_effect`, `move_device`, `duplicate_device`, `delete_device`, `set_routing`, `set_sidechain`, `re_enable_automation` |
| **Clips & notes** | `create_clip`, `create_audio_clip`, `add_notes_to_clip`, `clear_notes_from_clip`, `set_clip_name`, `set_clip_muted`, `set_clip_groove`, `delete_clip`, `fire_clip`, `stop_clip` |
| **Arrangement** | `duplicate_to_arrangement` (with `repeat`/`every_bars`, refuses to overwrite unless asked), `delete_arrangement_clip`, `delete_arrangement_clips`, `create_locator`, `back_to_arrangement`, `switch_to_arrangement_view` |
| **Clip automation** | `set_clip_envelope`, `clear_clip_envelope` (session clips; see limits) |
| **Song & transport** | `set_tempo`, `set_loop`, `set_song` (global groove amount, metronome), `set_arrangement_time`, `start_playback` (`bar="loop"` starts inside the loop), `stop_playback`, `create_midi_track`, `create_audio_track`, `set_track_name` |
| **Safety net** | `undo`, `redo`, `mark_undo`, `undo_to_mark`, `snapshot`, `snapshot_diff` |

### The undo model

Live has one undo history, shared by you and the assistant. So:

- `undo` only takes back as many steps as the MCP itself created since it started,
  and names each one (`undone: move_device [HAT CLOSED] 'Utility' 1 -> 7`). More
  only with `force=true` – that would hit your own steps.
- `mark_undo("before")` before a series, `undo_to_mark("before")` takes back exactly
  that series. With a snapshot at the mark, `undo_to_mark` first re-reads the set and
  **refuses** if something changed that no MCP command explains (your hand edit would
  otherwise be undone instead), and afterwards checks that the marked state is back.
- Solo, arm, loop, playhead and launching clips create **no** undo step in Live; the
  reply says so. Setting an automated value overrides the automation; `undo` restores
  the value but not the automation – use `re_enable_automation`.
- `delete_device`, the arrangement clip delete tools and `clear_clip_envelope` write the
  full previous state as JSON
  next to the server (`technoa_mcp/geloeschte_geraete/`, `technoa_mcp/geloeschte_clips/`),
  in case undo is not enough. Deleting a session clip (`delete_clip`) has no such backup.

### Snapshots

`snapshot` reads the whole set (tracks, routing, mixer, every device parameter, clips
with a content fingerprint, locators, grooves, playhead; ~8 s, changes nothing) and
stores it as JSON. `snapshot_diff` compares two of them – or one against "now" – and
lists changes by importance: tracks, routing, devices, automation, mixer, song,
parameters, clips. Tracks are matched by name, clips by start position, devices as a
chain.

Snapshots are stored per project in `snapshots/<project>/` next to `technoa_mcp/`, or
under `ABLETON_MCP_SNAPSHOT_DIR`. Without `project`, the project with the newest
snapshot is used, otherwise `default`.

## Installation

Tested on **Windows 11** with **Ableton Live 12.4.6 and 12.4.7** and Python 3.11.9,
with Claude Code as client. Other systems and Live versions are untested.

1. **Python environment** (from the repository folder):

   ```powershell
   py -3.11 -m venv venv
   venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. **Remote Script into Live** – with Live **closed**:

   ```powershell
   venv\Scripts\python.exe technoa_mcp\deploy_remote_script.py --check   # where would it go?
   venv\Scripts\python.exe technoa_mcp\deploy_remote_script.py           # install
   ```

   It writes only `<User Library>\Remote Scripts\AbletonMCP\__init__.py`. Default User
   Library: `Documents\Ableton\User Library` (macOS: `Music/Ableton/User Library`). If
   you moved it (Live: *Settings > Library*), pass `--user-library "<path>"`. An
   existing script there is backed up to `technoa_mcp/deploy_backups/` first;
   `--restore` puts the newest backup back.

3. **Enable it in Live:** start Live, *Settings > Link, Tempo & MIDI*, pick
   **AbletonMCP** as a Control Surface (Input/Output: None).

4. **Register the MCP server** with your client, e.g. a `.mcp.json` for Claude Code:

   ```json
   {
     "mcpServers": {
       "ableton": {
         "command": "C:\\path\\to\\repo\\venv\\Scripts\\python.exe",
         "args": ["C:\\path\\to\\repo\\technoa_mcp\\server.py"]
       }
     }
   }
   ```

   The same block works in Claude Desktop's `claude_desktop_config.json`.

Live reads Remote Scripts only at startup, so after every deploy restart Live.
Changes to `server.py`, `views.py` or `als_reader.py` only need a restart of the MCP
server.

**Coming from the original ableton-mcp:** both use the name `AbletonMCP` and port
9877, so use one or the other. The deploy script backs up the original before
replacing it.

**Removing it:** `deploy_remote_script.py --restore`, or delete the folder
`Remote Scripts\AbletonMCP` in your User Library and restart Live.

## Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `ABLETON_MCP_SNAPSHOT_DIR` | server | Where snapshots go (default: `snapshots/` in the repo) |
| `ABLETON_USER_LIBRARY` | deploy script | Live's User Library, if not in the default place |
| `TECHNOA_DELETED_DIR`, `TECHNOA_DELETED_CLIPS_DIR` | server | Where backups of deleted devices / clips go |
| `ABLETON_PORT` | server | Only for tests (fake Live on another port); the address stays `127.0.0.1` |

## Privacy and security

- No telemetry, no dataset recording, no network access beyond the local socket. The
  server depends on nothing but the MCP library.
- Server and Remote Script talk over **`127.0.0.1:9877` without authentication** –
  any program on your machine could send commands to Live while the script runs. The
  original allowed binding to other addresses via an environment variable; that is
  removed.
- `inspect_lom` (exploring Live's API) only reads properties, never calls methods, and
  refuses names starting with `_`.

## Known limits

Mostly Live API limits, found the hard way:

- **Arrangement track automation is unreachable** through the API (Live 12.4.7). Clip
  envelopes work on **session clips** only; arrangement clips return none. The way into
  the arrangement: write the curve into a session clip, then `duplicate_to_arrangement`.
  There the curve applies inside the clip; before and after it, the fader does.
- A new clip envelope starts with the parameter's current value at the clip start, and
  that first point cannot be overwritten step-wise – you can get a click at the loop
  restart. The reply warns and names the workaround.
- A clip groove **cannot be removed** via the API (`clip.groove = None` is rejected). A
  neutral 0 % groove in the pool is the workaround.
- Devices are managed on track level only, **not inside racks** (rack parameters can be
  read and set via `chain_path`). Device tools were verified with Live devices, not with
  plugins.
- `watch_meters` polls a few times per second; Live's own peak display read 0.6–0.8 dB
  higher. The meter-to-dBFS mapping was calibrated on one machine (measured range
  0.106–0.812; above that it is extrapolated and marked `~`).
- `start_playback` without `bar` starts at Live's start marker, not at the playhead or
  the loop. For measuring use `bar="loop"`.
- Loading an instrument from the browser can arm the track (Live's auto-arm); the
  tools report it, `disarm=true` undoes it.

## What was tested

- **Without Live:** 149 tests (`technoa_mcp/tests/`) against a fake Live that mimics the
  quirks met on the real thing (main-thread-only access, values that update one pass
  later, group track errors). They run the Remote Script over its socket and the whole
  chain over MCP stdio:

  ```powershell
  cd technoa_mcp
  ..\venv\Scripts\python.exe -m unittest discover -s tests
  ```

- **On a real set** (Live 12.4.6/12.4.7, Windows): each write command was read before,
  re-read after and undone; one undo step per command was confirmed for mixer, device
  parameters, notes, tempo, locators, tracks, clips, device management, groove,
  routing, sidechain, `duplicate_to_arrangement` with `repeat`, and clip volume
  envelopes. Snapshots and `undo_to_mark` (including the refusal on a hand edit) were
  used during real sessions.
- **Only in the fake Live:** `set_song`, `delete_arrangement_clips`, clip envelopes on
  device parameters/sends/pan, routing of returns and master, plugins in the device
  tools, drum racks, time signature changes.
- This public snapshot was cleaned up from the private repo (one feature tied to an
  unreleased plugin removed, paths made configurable, deploy script generalised). After
  the cleanup it was checked with the test suite only, not again against a running Live.

## Credits and license

MIT – see [LICENSE](LICENSE). Based on
[ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) by Siddharth Ahuja.
Not affiliated with or endorsed by Ableton.
