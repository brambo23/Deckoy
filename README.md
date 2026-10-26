# Deckoy

**A decoy Network Dock that keeps the Elgato Stream Deck app connected.**

The Elgato Stream Deck app randomly drops its session with a Stream Deck Network Dock and does not come back until it is restarted.
Deckoy sits between them: it is the only program talking to the real dock, and it presents itself to the Elgato app as a Network Dock
on the same PC. It answers the app instantly, hides dock-link drops from it, and re-announces itself if the app drops its session.

# **⚠️ THIS PROJECT WAS 100% CUCKCODED ⚠️**

![Are you sure? Yes.](docs/cuckcoded.png)

*Written entirely by Claude Code (AI), with a human nodding along. Read the code before you trust it.*

## How it works

```
 Elgato Stream Deck app            Deckoy (this PC)                 real Network Dock
 (your profiles, plugins)   --->   :5343 dock service       --->    + Stream Deck XL
                                   :20002 deck service
                                   mDNS "... (Deckoy)"
```

The Elgato app finds Deckoy through mDNS (`_elg._tcp`) like a real dock. Deckoy keeps one healthy connection to the real dock
(handshake, keepalives, reconnect in about a second, key images, brightness and sleep timeout re-sent after a reconnect) and answers
the Elgato app from its cache, so a hiccup on the dock side never reaches the app.

## Requirements

* Windows 10 or 11
* An Elgato Stream Deck **XL** behind a Stream Deck **Network Dock**. This is the only configuration tested; other decks are not
  supported for now (the key layout is fixed at 32 keys).
* The Elgato Stream Deck app
* The PC and the dock on the same LAN, with mDNS/Bonjour working (the Elgato app already relies on it)

## Install

Download `Deckoy-<version>-win64.zip` from the [Releases page](https://github.com/brambo23/Deckoy/releases), unzip it anywhere and run `Deckoy.exe`.
Windows SmartScreen may warn about an unsigned app: *More info* then *Run anyway*.

From source (Python 3.10+):

```
pip install -r requirements.txt
python -m deckoy
```

## Setup (first time)

1. **Quit the Elgato Stream Deck app completely** (tray icon, Quit). Also stop anything else that talks to the dock: only one
   program may be connected to the dock at a time.
2. **Start Deckoy.** If Windows Firewall asks, allow it on **Private networks** (the Elgato app on this PC connects to it).
3. Wait for **Real dock: connected** on the status page. If it stays on *connecting*, open **File > Settings**, enter your dock's
   IP address (see your router, or the device info in the Elgato app) and press OK.
4. **(Recommended) Stop the Elgato app from reaching the real dock directly**, so it can only ever find Deckoy. Read
   `tools\proxy-firewall-on.ps1` first, then run it from an **Administrator** PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\tools\proxy-firewall-on.ps1 -DockIp <your dock IP>
   ```

   It adds ONE outbound block rule (`StreamDeck.exe` to your dock's IP). Deckoy itself is not blocked. Undo it with
   `tools\proxy-firewall-off.ps1`.
5. **Add Deckoy in the Elgato app.** If your real dock is already listed as a device, remove it first. Start the Elgato Stream
   Deck app, open the device menu / **Add New Device** (the exact path may differ between app versions) and pick
   **Stream Deck Network Dock ... (Deckoy)**. If it does not appear, add a Network Dock manually by IP, using this PC's IP address.
6. The status page should now show **Elgato app: connected** and your profiles appear on the deck. Leave Deckoy running: it lives
   in the tray (**File > Settings > Start with Windows** launches it at login). Start Deckoy before the Elgato app.

## Status page and tray

| Item | Meaning |
|---|---|
| Real dock | Deckoy's own connection to the real dock |
| Elgato app | an Elgato app session is open on Deckoy |
| Drops hidden today | dock-side drops that happened while the Elgato app was connected and that it never saw |
| Elgato app sessions ended | established (> 3 s) sessions the Elgato app ended by itself (each is logged with its time) |
| Re-announces | how many times Deckoy withdrew and re-announced the deck over mDNS |
| Requests answered | requests Deckoy answered for the Elgato app from its cache |

The tray menu shows the same summary lines, plus *Reconnect* and a test action that drops the Elgato app's session on purpose to
exercise the re-announce path. Counters are kept in `%APPDATA%\Deckoy\proxy_stats.json`.

## Settings

Most of these should stay at their defaults.

| Setting | Meaning |
|---|---|
| Dock IP / Dock port | where the real dock is; blank / auto = find it by mDNS |
| Brightness, Sleep timeout | applied to the dock and re-sent after every reconnect |
| Start with Windows | a shortcut in your Startup folder (no registry, no admin) |
| Advertise address | the address announced via mDNS; blank = this PC's LAN address |
| Listen on all addresses | needed because the Elgato app tries every local address of this PC; only this PC can connect either way |
| Deck / Dock service port | local ports (default 20002 and 5343) |
| mDNS names | `... (Deckoy)` names (default) or the same names as the real dock |
| When the Elgato app drops | re-announce the deck (default), dock and deck, or only log it; on a new port or the same one; pause length |
| Forward unknown commands | off by default; see Safety |

Settings live in `%APPDATA%\Deckoy\settings.json`.

## Troubleshooting

* **The deck is not listed in the Elgato app:** check `deckoy.log` for `advertising ...` and the Elgato log for
  `mDNS online service` / `reject dock service`. If the app rejects the dock, try *mDNS names: Same names as the real dock*. Make sure
  the dock IP is set so Deckoy can copy the real dock's mDNS records; with generic fallback records (the log says so) the app
  may refuse it.
* **"Device already in use" / reject:** another program is connected to the real dock (the Elgato app, Touch Portal's
  SDDockBridge plugin, a second Deckoy). Quit it.
* **Stuck on connecting:** set the dock's IP in Settings; check that the PC can reach it.
* **Port in use:** change the deck / dock service ports in Settings.
* **The Elgato app ended its session and did not come back:** restart the Elgato app. Deckoy re-announces the deck over mDNS to try
  to bring the app back by itself, but this is experimental and **not yet confirmed** to work. What Deckoy does reliably is keep
  the dock link healthy and answer instantly; it cannot stop the app from aborting its own session.
* **Logs:** `%APPDATA%\Deckoy\logs\deckoy.log` (also via *File > Open config / log folder*); the Elgato app's log is
  `%APPDATA%\Elgato\StreamDeck\logs\StreamDeck.log`.

## Safety

* Deckoy sends the dock only a fixed allow-list of commands: handshake reads, brightness, sleep, images and keepalives. Anything else
  is acknowledged to the Elgato app, logged, and **not forwarded** (the *Forward unknown commands* setting changes that, and a bulk
  unknown transfer over 4 KB is blocked even then).
* **Never update the dock or deck firmware through Deckoy.** Quit Deckoy, remove the firewall rule, and update with the Elgato app
  connected directly.
* Deckoy's ports listen on this PC's addresses but accept connections **only from this PC**; other hosts are refused and logged.
  The dock protocol is plain unauthenticated TCP, exactly as the Elgato app uses it.
* Deckoy never changes the firewall by itself and never edits the Elgato app's configuration.

## Uninstall / go back to normal

1. Quit Deckoy (tray icon, Quit).
2. Run `tools\proxy-firewall-off.ps1` from an Administrator PowerShell.
3. Turn off *Start with Windows*, then delete the Deckoy folder and `%APPDATA%\Deckoy`.
4. In the Elgato app, remove the `(Deckoy)` device and add your real dock again.

## Building from source and running the tests

```
pip install -r requirements-dev.txt
python -m pytest
.\build.ps1
```

`build.ps1` runs the tests, generates the icon, builds `dist\Deckoy\Deckoy.exe` with PyInstaller, zips it to
`dist\Deckoy-<version>-win64.zip` and runs the quit tests against the built exe. The tests use mocks on localhost only.

## Credits

Deckoy is the proxy mode of DeckPad, split out into its own small app.

## Disclaimer

Deckoy is not affiliated with or endorsed by Elgato or Corsair. Stream Deck is their trademark. The dock protocol was reverse
engineered and is unofficial. Use at your own risk.

License: [MIT](LICENSE)
