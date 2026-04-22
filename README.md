# discord-mitm

A mitmproxy-based tool that intercepts Discord's HTTPS traffic to monitor, filter, and react to messages in real time. Includes **AEGIS** — a punishment module that triggers on keyword matches.

## How it works

1. `start.sh` launches `mitmdump` on port 8080 and restarts Discord routed through it with `--ignore-certificate-errors`.
2. `addon.py` inspects outgoing messages, slash commands, and interactions. It can block messages matching configurable keywords and trigger AEGIS.
3. `punish.py` (AEGIS) captures the webcam, maxes volume, disables input, plays a fullscreen rickroll, then locks the screen and sends evidence to Discord.
4. `stop.sh` kills mitmproxy and restarts Discord normally.

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
