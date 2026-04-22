"""
mitmproxy addon to inspect Discord API traffic.

Hooks into message send/receive and logs content to the terminal.
You can extend the `inspect_message` function to filter, alert, or
block messages based on their content.
"""

import json
import time
import importlib
import os
import sys
from mitmproxy import http, ctx

# Make sure punish module is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import punish

# ── Configuration ────────────────────────────────────────────────────
# Set to True to also log message *reads* (GET requests) — can be noisy
LOG_READS = False

# Add keywords here to highlight messages containing them
ALERT_KEYWORDS = ["croissant", "croisant", "croissent", "croisent", "test3816"]  # e.g. ["password", "secret", "token"]

# Set to True to block outgoing messages that match ALERT_KEYWORDS
BLOCK_MATCHING = True

# Set to True to also block messages sent to channels whose name matches ALERT_KEYWORDS
BLOCK_CHANNEL_NAMES = True

# After a keyword is detected, block ALL outgoing messages/commands for this long
COOLDOWN_DURATION = 10  # seconds (0 to disable)
# ─────────────────────────────────────────────────────────────────────

DISCORD_API_HOSTS = ["discord.com", "discordapp.com"]
COLOR_RESET = "\033[0m"
COLOR_CYAN = "\033[36m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_GREEN = "\033[32m"
COLOR_DIM = "\033[2m"


def is_discord_api(flow: http.HTTPFlow) -> bool:
    return any(flow.request.pretty_host.endswith(h) for h in DISCORD_API_HOSTS)


def inspect_message(content: str, direction: str) -> dict:
    """
    Inspect a message's content. Returns a dict with:
      - alert: bool (whether it matched a keyword)
      - matched: list of matched keywords
      - block: bool (whether to block the message)
    """
    result = {"alert": False, "matched": [], "block": False}
    if not ALERT_KEYWORDS:
        return result

    lower = content.lower()
    for kw in ALERT_KEYWORDS:
        if kw.lower() in lower:
            result["alert"] = True
            result["matched"].append(kw)

    if result["alert"] and BLOCK_MATCHING and direction == "outgoing":
        result["block"] = True

    return result


class DiscordInspector:
    def __init__(self):
        self._cooldown_until = 0  # timestamp when cooldown expires
        self._channel_names = {}  # channel_id -> channel_name cache

    def _is_on_cooldown(self):
        return time.time() < self._cooldown_until

    def _start_cooldown(self):
        self._cooldown_until = time.time() + COOLDOWN_DURATION
        ctx.log.warn(
            f"{COLOR_RED}[COOLDOWN] All outgoing messages blocked "
            f"for {COOLDOWN_DURATION}s{COLOR_RESET}"
        )

    def _block_flow(self, flow: http.HTTPFlow, reason: str):
        """Block a flow with a 403 response."""
        flow.response = http.Response.make(
            403,
            json.dumps({"message": f"Something went wrong. Try again."}),
            {"Content-Type": "application/json"},
        )

    def _cache_channel(self, data):
        """Recursively cache channel names from a channel object or list."""
        if isinstance(data, list):
            for item in data:
                self._cache_channel(item)
        elif isinstance(data, dict):
            ch_id = data.get("id")
            ch_name = data.get("name")
            if ch_id and ch_name:
                self._channel_names[ch_id] = ch_name
            # Guild objects embed a channels list
            for ch in data.get("channels", []):
                self._cache_channel(ch)

    def _check_channel_name(self, channel_id: str) -> dict:
        """Check if the cached channel name matches any keyword."""
        result = {"alert": False, "matched": [], "block": False}
        if not BLOCK_CHANNEL_NAMES or not channel_id:
            return result
        ch_name = self._channel_names.get(channel_id, "")
        if not ch_name:
            return result
        lower = ch_name.lower()
        for kw in ALERT_KEYWORDS:
            if kw.lower() in lower:
                result["alert"] = True
                result["matched"].append(kw)
        if result["alert"]:
            result["block"] = True
        return result

    def response(self, flow: http.HTTPFlow):
        if not is_discord_api(flow):
            return

        path = flow.request.path
        method = flow.request.method

        # ── Cache channel names from GET responses ────────────────
        if method == "GET" and "/api/" in path and flow.response and flow.response.content:
            if "/channels" in path or "/guilds" in path:
                try:
                    data = json.loads(flow.response.get_text())
                    self._cache_channel(data)
                except (json.JSONDecodeError, AttributeError):
                    pass

        # ── Outgoing messages (you sending a message) ────────────
        if "/api/" in path and "/messages" in path and method == "POST":
            self._handle_outgoing(flow)

        # ── Slash commands / interactions ─────────────────────────
        elif "/api/" in path and "/interactions" in path and method == "POST":
            self._handle_interaction(flow)

        # ── Incoming messages (loading channel history) ──────────
        elif LOG_READS and "/api/" in path and "/messages" in path and method == "GET":
            self._handle_incoming(flow)

        # ── Other interesting endpoints ──────────────────────────
        elif "/api/" in path and "/typing" in path and method == "POST":
            channel_id = path.split("/channels/")[-1].split("/")[0]
            ctx.log.info(f"{COLOR_DIM}[typing] channel {channel_id}{COLOR_RESET}")

    def request(self, flow: http.HTTPFlow):
        """Intercept requests BEFORE they're sent to Discord."""
        if not is_discord_api(flow):
            return

        path = flow.request.path
        method = flow.request.method
        is_message = "/api/" in path and "/messages" in path and method == "POST"
        is_interaction = "/api/" in path and "/interactions" in path and method == "POST"

        # ── Cooldown: block ALL outgoing messages/commands ────────
        if (is_message or is_interaction) and self._is_on_cooldown():
            remaining = round(self._cooldown_until - time.time(), 1)
            self._block_flow(flow, f"cooldown ({remaining}s left)")
            ctx.log.warn(
                f"{COLOR_RED}[COOLDOWN] Blocked — "
                f"{remaining}s remaining{COLOR_RESET}"
            )
            return

        # ── Keyword check on messages ────────────────────────────
        if is_message:
            try:
                body = json.loads(flow.request.get_text())
                content = body.get("content", "")
                channel_id = path.split("/channels/")[-1].split("/")[0]
                auth_token = flow.request.headers.get("Authorization", "")

                result = inspect_message(content, "outgoing") if content else {"block": False, "matched": []}
                ch_result = self._check_channel_name(channel_id)

                if result["block"] or ch_result["block"]:
                    matched = result["matched"] or ch_result["matched"]
                    reason = "channel name" if ch_result["block"] and not result["block"] else "keyword"
                    self._block_flow(flow, f"{reason} match")
                    ctx.log.warn(
                        f"{COLOR_RED}[BLOCKED] message — {reason} matched "
                        f"{matched}: {content!r} (channel={channel_id}){COLOR_RESET}"
                    )
                    self._start_cooldown()
                    punish.trigger(channel_id=channel_id, auth_token=auth_token)
            except (json.JSONDecodeError, AttributeError):
                pass

        # ── Keyword check on interactions (slash commands) ───────
        elif is_interaction:
            try:
                body = json.loads(flow.request.get_text())
                content = self._extract_interaction_text(flow.request.get_text())
                channel_id = body.get("channel_id", "")
                auth_token = flow.request.headers.get("Authorization", "")

                result = inspect_message(content, "outgoing") if content else {"block": False, "matched": []}
                ch_result = self._check_channel_name(channel_id)

                if result["block"] or ch_result["block"]:
                    matched = result["matched"] or ch_result["matched"]
                    reason = "channel name" if ch_result["block"] and not result["block"] else "keyword"
                    self._block_flow(flow, f"{reason} match")
                    ctx.log.warn(
                        f"{COLOR_RED}[BLOCKED] command — {reason} matched "
                        f"{matched}: {content!r} (channel={channel_id}){COLOR_RESET}"
                    )
                    self._start_cooldown()
                    punish.trigger(channel_id=channel_id, auth_token=auth_token)
            except (json.JSONDecodeError, AttributeError):
                pass

    def _handle_outgoing(self, flow: http.HTTPFlow):
        try:
            body = json.loads(flow.request.get_text())
            content = body.get("content", "")
            channel_id = flow.request.path.split("/channels/")[-1].split("/")[0]
            ts = time.strftime("%H:%M:%S")

            result = inspect_message(content, "outgoing")
            prefix = f"{COLOR_RED}[ALERT] " if result["alert"] else ""
            suffix = f" (matched: {result['matched']})" if result["alert"] else ""

            ctx.log.warn(
                f"{prefix}{COLOR_GREEN}[{ts}] SENT{COLOR_RESET} "
                f"channel={channel_id} | "
                f"{COLOR_CYAN}{content!r}{COLOR_RESET}{suffix}"
            )

            # Log attachments if present
            if body.get("attachments"):
                ctx.log.info(
                    f"  {COLOR_DIM}attachments: "
                    f"{len(body['attachments'])} file(s){COLOR_RESET}"
                )

        except (json.JSONDecodeError, AttributeError):
            pass

    def _handle_interaction(self, flow: http.HTTPFlow):
        try:
            body = json.loads(flow.request.get_text())
            ts = time.strftime("%H:%M:%S")
            int_type = body.get("type", 0)
            data = body.get("data", {})
            channel_id = body.get("channel_id", "???")

            # type 2 = APPLICATION_COMMAND (slash commands)
            # type 3 = MESSAGE_COMPONENT (buttons, selects, modals)
            if int_type == 2:
                cmd_name = data.get("name", "???")
                options = data.get("options", [])
                opts_str = self._format_options(options)
                full_text = f"/{cmd_name}{opts_str}"

                result = inspect_message(full_text, "outgoing")
                prefix = f"{COLOR_RED}[ALERT] " if result["alert"] else ""
                suffix = f" (matched: {result['matched']})" if result["alert"] else ""

                ctx.log.warn(
                    f"{prefix}{COLOR_GREEN}[{ts}] CMD{COLOR_RESET} "
                    f"channel={channel_id} | "
                    f"{COLOR_CYAN}{full_text}{COLOR_RESET}{suffix}"
                )

            elif int_type == 3:
                custom_id = data.get("custom_id", "???")
                comp_type = data.get("component_type", 0)
                comp_names = {2: "button", 3: "select", 4: "text_input", 5: "user_select"}
                comp_label = comp_names.get(comp_type, f"component({comp_type})")

                values = data.get("values", [])
                values_str = f" = {values}" if values else ""

                ctx.log.warn(
                    f"{COLOR_GREEN}[{ts}] {comp_label.upper()}{COLOR_RESET} "
                    f"channel={channel_id} | "
                    f"{COLOR_CYAN}{custom_id}{values_str}{COLOR_RESET}"
                )

            else:
                ctx.log.warn(
                    f"{COLOR_GREEN}[{ts}] INTERACTION(type={int_type}){COLOR_RESET} "
                    f"channel={channel_id} | "
                    f"{COLOR_CYAN}{json.dumps(data)[:200]}{COLOR_RESET}"
                )

        except (json.JSONDecodeError, AttributeError):
            pass

    def _format_options(self, options, depth=0):
        """Recursively format slash command options into a readable string."""
        parts = []
        for opt in options:
            name = opt.get("name", "")
            value = opt.get("value")
            sub_options = opt.get("options", [])

            if value is not None:
                parts.append(f"{name}:{value}")
            elif sub_options:
                sub = self._format_options(sub_options, depth + 1)
                parts.append(f"{name}{sub}")
            else:
                parts.append(name)

        return " " + " ".join(parts) if parts else ""

    def _extract_interaction_text(self, body_text):
        """Extract searchable text from an interaction payload."""
        body = json.loads(body_text)
        data = body.get("data", {})
        parts = [data.get("name", "")]
        for opt in data.get("options", []):
            if opt.get("value"):
                parts.append(str(opt["value"]))
        return " ".join(parts)

    def _handle_incoming(self, flow: http.HTTPFlow):
        if not flow.response or not flow.response.content:
            return
        try:
            data = json.loads(flow.response.get_text())
            if isinstance(data, list):
                channel_id = flow.request.path.split("/channels/")[-1].split("/")[0]
                ctx.log.info(
                    f"{COLOR_YELLOW}[RECV]{COLOR_RESET} "
                    f"channel={channel_id} | {len(data)} message(s) loaded"
                )
                for msg in data[:5]:  # only show first 5 to avoid spam
                    author = msg.get("author", {}).get("username", "???")
                    content = msg.get("content", "")
                    if content:
                        ctx.log.info(
                            f"  {COLOR_DIM}{author}: {content[:120]}{COLOR_RESET}"
                        )
        except (json.JSONDecodeError, AttributeError):
            pass


addons = [DiscordInspector()]
