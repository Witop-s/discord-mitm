"""
AEGIS — punishment module for discord-mitm.

When triggered:
  1. Captures webcam in 720p (photos or video) as raw evidence
  2. Saves/restores volume — maxes out during punishment
  3. Disables all keyboard/mouse input via xinput
  4. Opens nevergonnagiveyouup.mp4 fullscreen
  5. Plays for PLAY_DURATION seconds
  6. Compresses capture for Discord (<10MB), sends AEGIS message
  7. Locks the screen

Fail-safe: press "p" 3 times consecutively to abort.
  (Uses evdev to read /dev/input directly — works even with xinput disabled)
"""

import subprocess
import threading
import time
import os
import sys
import re
import json
import urllib.request
import urllib.error
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_PATH = os.path.join(SCRIPT_DIR, "nevergonnagiveyouup.mp4")
CAPTURES_DIR = os.path.join(SCRIPT_DIR, "captures")
PLAY_DURATION = 50   # seconds — rickroll playback time
RECORD_DURATION = 60 # seconds — total webcam recording time (delay + playback)
ABORT_KEY = "KEY_P"
ABORT_COUNT = 3
ABORT_WINDOW = 2.0   # seconds — all 3 presses must happen within this window
PUNISH_DELAY = 10    # seconds — capture starts immediately, video/lock waits this long

# ── Webcam config ────────────────────────────────────────────────────
# "photos" = 4 snapshots spread across the punishment duration
# "video"  = continuous recording for the full duration
CAPTURE_MODE = "video"  # "photos" or "video"
WEBCAM_DEVICE = "/dev/video0"
WEBCAM_RAW_RES = "1280x720"   # raw capture resolution (720p)
WEBCAM_RAW_FPS = 24
NUM_PHOTOS = 4
# ─────────────────────────────────────────────────────────────────────

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
AEGIS_MSG = "# :robot: *AEGIS*: UNAUTHORIZED :croissant::croissant::croissant:ATTEMPT DENIED. :middle_finger:"


def _get_xinput_device_ids():
    """Get all slave keyboard and pointer device IDs (skip virtual core devices)."""
    try:
        out = subprocess.check_output(["xinput", "list", "--short"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    ids = []
    for line in out.splitlines():
        if "slave" not in line:
            continue
        if "XTEST" in line:
            continue
        for part in line.split():
            if part.startswith("id="):
                try:
                    ids.append(int(part.split("=")[1]))
                except ValueError:
                    pass
    return ids


def _get_current_volume():
    """Get current volume level and mute state. Returns (backend, volume%, is_muted) or None."""
    # Try pactl first
    try:
        out = subprocess.check_output(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"], text=True, timeout=3
        )
        match = re.search(r"(\d+)%", out)
        volume = int(match.group(1)) if match else None

        mute_out = subprocess.check_output(
            ["pactl", "get-sink-mute", "@DEFAULT_SINK@"], text=True, timeout=3
        )
        is_muted = "yes" in mute_out.lower()

        if volume is not None:
            return ("pactl", volume, is_muted)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    # Try wpctl fallback
    try:
        out = subprocess.check_output(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], text=True, timeout=3
        )
        match = re.search(r"([\d.]+)", out)
        volume = round(float(match.group(1)) * 100) if match else None
        is_muted = "[MUTED]" in out.upper()

        if volume is not None:
            return ("wpctl", volume, is_muted)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    return None


class Punishment:
    def __init__(self, channel_id=None, auth_token=None):
        self.aborted = False
        self.mpv_proc = None
        self._ffmpeg_proc = None
        self._p_timestamps = []
        self._listener_thread = None
        self._disabled_devices = []
        self._saved_volume = None  # (backend, volume%, is_muted)
        self._channel_id = channel_id
        self._auth_token = auth_token
        self._capture_path = None  # path to the raw capture

    def run(self):
        """Execute the full AEGIS punishment sequence. Blocks until done or aborted."""
        if not os.path.isfile(VIDEO_PATH):
            print(f"[AEGIS] Video not found: {VIDEO_PATH}")
            return

        print(f"[AEGIS] Engaged — press '{ABORT_KEY}' x{ABORT_COUNT} to abort")

        # Ensure captures directory exists
        os.makedirs(CAPTURES_DIR, exist_ok=True)

        # Step 1: Start webcam capture immediately (before delay)
        self._start_capture()

        # Step 2: Start evdev key listener
        self._start_evdev_listener()

        # Step 3: Wait for delay — capture is already rolling
        if PUNISH_DELAY > 0:
            print(f"[AEGIS] Capturing silently for {PUNISH_DELAY}s before striking...")
            delay_start = time.time()
            while time.time() - delay_start < PUNISH_DELAY:
                if self.aborted:
                    print("[AEGIS] Aborted during delay!")
                    self._stop_capture()
                    return
                time.sleep(0.2)

        # Step 4: Save current volume, then max it
        self._save_and_max_volume()

        # Step 5: Disable all input devices (keyboard + mouse)
        self._disable_input()

        # Step 6: Launch mpv fullscreen
        try:
            self.mpv_proc = subprocess.Popen(
                [
                    "mpv",
                    "--fullscreen",
                    "--ontop",
                    "--no-terminal",
                    "--really-quiet",
                    "--loop=inf",
                    "--no-input-default-bindings",
                    "--no-osc",
                    "--cursor-autohide=always",
                    "--volume=100",
                    VIDEO_PATH,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print("[AEGIS] mpv not found, aborting")
            self._stop_capture()
            self._restore_input()
            self._restore_volume()
            return

        # Wait for duration or abort, taking photos at intervals if needed
        start = time.time()
        photo_times = self._get_photo_schedule()

        while time.time() - start < PLAY_DURATION:
            if self.aborted:
                print("[AEGIS] Aborted! You're safe... this time.")
                self._cleanup()
                return

            # Take scheduled photos
            elapsed = time.time() - start
            for i, t in enumerate(photo_times):
                if t is not None and elapsed >= t:
                    self._take_photo(i)
                    photo_times[i] = None  # mark as taken

            time.sleep(0.2)

        # Time's up — kill video first, then handle the rest
        print("[AEGIS] Time's up")
        self._kill_mpv()
        self._stop_capture()
        self._send_aegis_message()  # compress + upload (mpv already dead)
        self._cleanup()
        self._lock_screen()

    def _get_photo_schedule(self):
        """Return list of timestamps (seconds) for when to take each photo."""
        if CAPTURE_MODE != "photos":
            return []
        interval = PLAY_DURATION / NUM_PHOTOS
        return [i * interval for i in range(NUM_PHOTOS)]

    def _start_capture(self):
        """Start webcam capture (video recording or prepare for photos)."""
        if CAPTURE_MODE == "video":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Record raw as .mkv — container handles interrupted writes better,
            # and we'll transcode to .mp4 for Discord anyway
            output = os.path.join(CAPTURES_DIR, f"webcam_{ts}_raw.mkv")
            self._capture_path = output
            try:
                self._ffmpeg_proc = subprocess.Popen(
                    [
                        "ffmpeg", "-y",
                        "-f", "v4l2",
                        "-framerate", str(WEBCAM_RAW_FPS),
                        "-video_size", WEBCAM_RAW_RES,
                        "-i", WEBCAM_DEVICE,
                        "-f", "pulse",
                        "-i", "default",
                        "-t", str(RECORD_DURATION),
                        "-c:v", "libx264",
                        "-preset", "ultrafast",
                        "-crf", "18",  # high quality raw
                        "-c:a", "aac",
                        "-b:a", "128k",
                        output,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[AEGIS] Recording 720p webcam → {output}")
            except FileNotFoundError:
                print("[AEGIS] ffmpeg not found — webcam capture skipped")
        elif CAPTURE_MODE == "photos":
            print(f"[AEGIS] Will capture {NUM_PHOTOS} photos during punishment")

    def _take_photo(self, index):
        """Capture a single photo from the webcam."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = os.path.join(CAPTURES_DIR, f"photo_{ts}_{index}.jpg")
        self._capture_path = output  # track latest photo
        try:
            subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "v4l2",
                    "-video_size", WEBCAM_RAW_RES,
                    "-i", WEBCAM_DEVICE,
                    "-frames:v", "1",
                    output,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[AEGIS] Captured photo {index + 1}/{NUM_PHOTOS} → {output}")
        except FileNotFoundError:
            pass

    def _stop_capture(self):
        """Stop webcam recording if running."""
        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
            self._ffmpeg_proc.terminate()
            try:
                self._ffmpeg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()

    def _compress_for_discord(self):
        """
        Compress the raw capture for Discord upload (<10MB).
        Strategy:
          1. Re-encode at 640x480 with reasonable quality
          2. If still > 10MB, trim duration until it fits
        Returns path to the compressed file, or None.
        """
        if not self._capture_path or not os.path.isfile(self._capture_path):
            return None

        # Photos don't need compression — just check size
        if not self._capture_path.endswith(".mkv"):
            size = os.path.getsize(self._capture_path)
            return self._capture_path if 0 < size <= MAX_UPLOAD_SIZE else None

        raw_size = os.path.getsize(self._capture_path)
        if raw_size == 0:
            return None

        print(f"[AEGIS] Raw capture: {raw_size // 1024}KB")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        discord_path = os.path.join(CAPTURES_DIR, f"webcam_{ts}_discord.mp4")

        # Pass 1: re-encode keeping 720p, just lower quality/bitrate
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", self._capture_path,
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "28",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    discord_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"[AEGIS] Compression failed: {e}")
            return None

        size = os.path.getsize(discord_path) if os.path.isfile(discord_path) else 0
        if 0 < size <= MAX_UPLOAD_SIZE:
            print(f"[AEGIS] Compressed: {size // 1024}KB — fits Discord")
            return discord_path

        # Pass 2: still too big — calculate max duration that fits
        # Get duration of compressed file
        try:
            probe = subprocess.check_output(
                [
                    "ffprobe", "-v", "quiet",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    discord_path,
                ],
                text=True, timeout=10,
            )
            duration = float(probe.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            print("[AEGIS] Could not probe duration — skipping attachment")
            return None

        if duration <= 0:
            return None

        # Calculate what fraction of the video fits in 10MB
        bitrate_per_sec = size / duration
        max_duration = MAX_UPLOAD_SIZE / bitrate_per_sec
        max_duration = max(5, int(max_duration))  # at least 5s

        print(f"[AEGIS] Still {size // 1024}KB — trimming to {max_duration}s")

        trimmed_path = os.path.join(CAPTURES_DIR, f"webcam_{ts}_discord_trimmed.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", self._capture_path,
                    "-t", str(max_duration),
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "28",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    trimmed_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        # Clean up the oversized version
        try:
            os.remove(discord_path)
        except OSError:
            pass

        final_size = os.path.getsize(trimmed_path) if os.path.isfile(trimmed_path) else 0
        if 0 < final_size <= MAX_UPLOAD_SIZE:
            print(f"[AEGIS] Trimmed: {final_size // 1024}KB — fits Discord")
            return trimmed_path

        print("[AEGIS] Still too large after trim — skipping attachment")
        return None

    def _save_and_max_volume(self):
        """Save current volume state, then unmute and set to 100%."""
        self._saved_volume = _get_current_volume()
        if self._saved_volume:
            backend, vol, muted = self._saved_volume
            state = f"{vol}%" + (" (muted)" if muted else "")
            print(f"[AEGIS] Saved volume: {state} [{backend}]")
        else:
            print("[AEGIS] Could not read current volume — will not restore")

        print("[AEGIS] Setting volume to MAX")
        for cmd in [
            ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"],
        ]:
            try:
                subprocess.run(cmd, timeout=3, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        for cmd in [
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.0"],
        ]:
            try:
                subprocess.run(cmd, timeout=3, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    def _restore_volume(self):
        """Restore volume to what it was before punishment."""
        if not self._saved_volume:
            return

        backend, vol, was_muted = self._saved_volume
        print(f"[AEGIS] Restoring volume to {vol}%" +
              (" (muted)" if was_muted else ""))

        if backend == "pactl":
            cmds = [
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{vol}%"],
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@",
                 "1" if was_muted else "0"],
            ]
        else:
            cmds = [
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@",
                 str(vol / 100.0)],
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@",
                 "1" if was_muted else "0"],
            ]

        for cmd in cmds:
            try:
                subprocess.run(cmd, timeout=3, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    def _disable_input(self):
        """Disable all keyboard and mouse devices via xinput."""
        self._disabled_devices = _get_xinput_device_ids()
        for dev_id in self._disabled_devices:
            try:
                subprocess.run(["xinput", "disable", str(dev_id)],
                               timeout=2, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        if self._disabled_devices:
            print(f"[AEGIS] Disabled {len(self._disabled_devices)} input device(s)")

    def _restore_input(self):
        """Re-enable all disabled input devices."""
        for dev_id in self._disabled_devices:
            try:
                subprocess.run(["xinput", "enable", str(dev_id)],
                               timeout=2, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        if self._disabled_devices:
            print(f"[AEGIS] Re-enabled {len(self._disabled_devices)} input device(s)")
        self._disabled_devices = []

    def _start_evdev_listener(self):
        """
        Read key events directly from /dev/input using evdev.
        This bypasses X11 entirely, so it works even when xinput devices
        are disabled (xinput only disables the X11 -> app forwarding).
        """
        self._listener_thread = threading.Thread(target=self._evdev_loop, daemon=True)
        self._listener_thread.start()

    def _evdev_loop(self):
        try:
            import evdev
            from evdev import ecodes

            devices = []
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    caps = dev.capabilities()
                    if ecodes.EV_KEY in caps:
                        devices.append(dev)
                except (PermissionError, OSError):
                    continue

            if not devices:
                print("[AEGIS] No evdev devices found — failsafe unavailable")
                return

            print(f"[AEGIS] Failsafe listening on {len(devices)} device(s)")

            import selectors
            sel = selectors.DefaultSelector()
            for dev in devices:
                sel.register(dev, selectors.EVENT_READ)

            while not self.aborted:
                events = sel.select(timeout=0.3)
                for key, _ in events:
                    dev = key.fileobj
                    try:
                        for event in dev.read():
                            if event.type == ecodes.EV_KEY and event.value == 1:
                                key_name = ecodes.KEY.get(event.code, "")
                                if key_name == ABORT_KEY:
                                    now = time.time()
                                    self._p_timestamps.append(now)
                                    self._p_timestamps = [
                                        t for t in self._p_timestamps
                                        if now - t <= ABORT_WINDOW
                                    ]
                                    remaining = ABORT_COUNT - len(self._p_timestamps)
                                    if remaining > 0:
                                        print(f"[AEGIS] 'p' detected — "
                                              f"{remaining} more to abort")
                                    if len(self._p_timestamps) >= ABORT_COUNT:
                                        self.aborted = True
                                        return
                                else:
                                    self._p_timestamps.clear()
                    except (OSError, IOError):
                        continue

            sel.close()
        except Exception as e:
            print(f"[AEGIS] evdev listener error: {e} — failsafe unavailable")

    def _kill_mpv(self):
        """Kill mpv if running."""
        if self.mpv_proc and self.mpv_proc.poll() is None:
            self.mpv_proc.terminate()
            try:
                self.mpv_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mpv_proc.kill()

    def _cleanup(self):
        """Restore input, volume, kill mpv, stop capture."""
        self._restore_input()
        self._restore_volume()
        self._stop_capture()
        self._kill_mpv()

    def _send_aegis_message(self):
        """Send the AEGIS message (+ compressed webcam capture) to the offending channel."""
        if not self._channel_id or not self._auth_token:
            print("[AEGIS] No channel/token — skipping message")
            return

        # Compress capture for Discord upload
        attach_path = self._compress_for_discord()

        url = f"https://discord.com/api/v9/channels/{self._channel_id}/messages"

        try:
            # Bypass mitmproxy so cooldown doesn't block us
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            if attach_path:
                self._send_multipart(url, attach_path)
            else:
                self._send_text_only(url)
        except Exception as e:
            print(f"[AEGIS] Failed to send message: {e}")

    def _send_text_only(self, url):
        """Send a text-only message to Discord."""
        payload = json.dumps({"content": AEGIS_MSG}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": self._auth_token,
                "Content-Type": "application/json",
                "User-Agent": "AEGIS",
            },
            method="POST",
        )
        with self._opener.open(req, timeout=10) as resp:
            print(f"[AEGIS] Message sent (status {resp.status})")

    def _send_multipart(self, url, file_path):
        """Send a message with a file attachment using multipart/form-data."""
        boundary = f"----AEGISBoundary{int(time.time())}"
        filename = os.path.basename(file_path)

        parts = []

        payload = json.dumps({"content": AEGIS_MSG})
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"{payload}\r\n"
        )

        mime = "video/mp4" if file_path.endswith(".mp4") else "image/jpeg"
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        )

        with open(file_path, "rb") as f:
            file_data = f.read()

        body = b""
        for part in parts:
            body += part.encode()
        body += file_header.encode()
        body += file_data
        body += f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": self._auth_token,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "AEGIS",
            },
            method="POST",
        )
        with self._opener.open(req, timeout=30) as resp:
            print(f"[AEGIS] Message + capture sent (status {resp.status})")

    def _lock_screen(self):
        """Lock the screen using whatever's available."""
        lock_cmds = [
            ["loginctl", "lock-session"],
            ["xdg-screensaver", "lock"],
            ["dm-tool", "lock"],
        ]
        for cmd in lock_cmds:
            try:
                subprocess.run(cmd, timeout=5)
                return
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        print("[AEGIS] Could not lock screen — no lock command worked")


def trigger(channel_id=None, auth_token=None):
    """Non-blocking trigger — runs punishment in a separate thread."""
    t = threading.Thread(target=_run, args=(channel_id, auth_token), daemon=True)
    t.start()


def _run(channel_id=None, auth_token=None):
    p = Punishment(channel_id=channel_id, auth_token=auth_token)
    p.run()


# Allow running standalone for testing: python3 punish.py
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test AEGIS punishment sequence")
    parser.add_argument("--mode", choices=["photos", "video"], default=CAPTURE_MODE,
                        help="Webcam capture mode (default: video)")
    args = parser.parse_args()
    CAPTURE_MODE = args.mode
    print(f"Testing AEGIS sequence (capture: {CAPTURE_MODE})...")
    p = Punishment()
    p.run()
