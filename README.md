# discord-mitm

A mitmproxy-based tool that intercepts Discord's HTTPS traffic to monitor, filter, and react to messages in real time. Includes **AEGIS** — a punishment module that triggers on keyword matches.

## How it works

1. `start.sh` launches `mitmdump` on port 8080 and restarts Discord routed through it with `--ignore-certificate-errors`.
2. `addon.py` inspects outgoing messages, slash commands, and interactions. It can block messages matching configurable keywords and trigger AEGIS.
3. `punish.py` (AEGIS) captures the webcam, maxes volume, disables input, plays a fullscreen rickroll, then locks the screen and sends evidence to Discord.
4. `stop.sh` kills mitmproxy and restarts Discord normally.

## How the interception works

Discord is built on **Electron**, which uses a Chromium engine under the hood. Like any Chromium-based app, it supports being launched with command-line flags that control its networking behavior.

`start.sh` exploits two of those flags:

```bash
discord \
  --proxy-server="http://127.0.0.1:8080" \
  --ignore-certificate-errors
```

- `--proxy-server` tells Chromium to route **all** HTTP and HTTPS requests through mitmproxy instead of connecting directly to Discord's servers.
- `--ignore-certificate-errors` makes Chromium accept mitmproxy's self-signed TLS certificate without complaining. This is necessary because mitmproxy performs a TLS man-in-the-middle: it terminates the SSL connection from Discord, reads the plaintext, then re-encrypts it toward Discord's servers using its own CA. Without this flag, Chromium would reject that certificate and refuse to connect.

Once traffic flows through mitmproxy, `addon.py` hooks into two events:
- `request`: fired **before** the request is sent — used to block messages in real time.
- `response`: fired **after** Discord's server replies — used to read outgoing message confirmations and cache channel metadata.

### What can and cannot be intercepted

Discord's REST API (sending messages, loading history, slash commands) goes over HTTPS and is fully visible through the proxy.

Real-time incoming messages however travel over a **WebSocket** connection (`wss://gateway.discord.gg`). mitmproxy can technically intercept WebSocket frames, but `addon.py` currently does not implement WebSocket parsing — so live incoming messages are **not** captured, only the REST calls are.

### Why Snap (and apt) installs don't work

The interception relies on being able to launch Discord with arbitrary Chromium flags. This breaks with Snap for several reasons:

- **Sandboxing**: Snap packages run inside a strict AppArmor/seccomp confinement profile. The sandbox controls what the process can access, and Snap's own launcher script wraps the real binary — flags passed to the `/snap/bin/discord` shim are not guaranteed to be forwarded as raw Chromium arguments.
- **Binary path**: `start.sh` hardcodes `/usr/bin/discord` and kills `/usr/share/discord/Discord`. Snap installs the binary under `/snap/discord/<revision>/discord` and the path changes with every update, so the script can't find or kill it reliably.
- **Automatic updates**: Snap Discord can auto-update and restart itself outside of the script's control, potentially re-launching without the proxy flags mid-session.

The `apt` version (if available through third-party repos) has similar path inconsistencies and may also wrap the binary in a way that strips or ignores custom flags.

The **official `.deb`** from discord.com installs the binary directly at `/usr/bin/discord` with no wrapper, so the flags are passed straight to Chromium as intended.

## Requirements

### OS
- **Linux only** — relies on X11 (`xinput`), `v4l2` (webcam), PulseAudio/PipeWire (`pactl`/`wpctl`), and `evdev`.

### Discord version
- Must be installed from the **official `.deb` package** from [discord.com](https://discord.com/download).
- `apt` (`discord`) and Snap installs **do not work** — the binary must be at `/usr/bin/discord` and the app at `/usr/share/discord/Discord`.
- To install: `sudo dpkg -i discord.deb`

### Dependencies

| Tool | Purpose |
|---|---|
| `mitmproxy` / `mitmdump` | Core proxy engine |
| `python3` + `evdev` | Addon + failsafe key listener |
| `mpv` | Fullscreen video playback |
| `ffmpeg` | Webcam capture + compression |
| `xinput` | Disable/re-enable input devices |
| `pactl` or `wpctl` | Volume control |

Install Python deps:
```bash
pip install mitmproxy evdev
```

## Usage

```bash
# Start (proxy + Discord)
./start.sh

# Stop and roll back to normal Discord
./stop.sh

# Test AEGIS punishment standalone
python3 punish.py
python3 punish.py --mode photos
```

## Configuration

Edit `addon.py` to tune behavior:

```python
ALERT_KEYWORDS    = ["word1", "word2"]  # triggers on these words
BLOCK_MATCHING    = True   # block outgoing messages that match
BLOCK_CHANNEL_NAMES = True # also block if channel name matches
COOLDOWN_DURATION = 10     # seconds to block all messages after a match
LOG_READS         = False  # log incoming message loads (noisy)
```

Edit `punish.py` for AEGIS settings:

```python
PLAY_DURATION   = 50      # seconds of rickroll playback
PUNISH_DELAY    = 10      # seconds of silent webcam capture before striking
CAPTURE_MODE    = "video" # "video" or "photos"
WEBCAM_DEVICE   = "/dev/video0"
```

## Limitations

- **Linux / X11 only** — will not work on Wayland without modifications (`xinput` is X11-specific).
- **Discord `.deb` required** — hardcoded path `/usr/bin/discord`. Snap/Flatpak/AppImage installs won't work.
- **Webcam required** for AEGIS capture — if `/dev/video0` is unavailable, capture is skipped gracefully.
- **Root or input group permissions** needed for `evdev` to read `/dev/input` devices (failsafe abort key).
- **No real-time messages** — Discord uses WebSockets for incoming messages; only REST API calls (sending messages, loading history) are intercepted.
- The proxy uses `--ignore-certificate-errors`, which means **all** TLS errors in Discord are silently bypassed for the duration.

## Failsafe

During AEGIS punishment, press **P three times within 2 seconds** to abort. This is read directly from `/dev/input` via `evdev` and works even when `xinput` has disabled the keyboard.
