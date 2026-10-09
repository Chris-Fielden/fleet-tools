"""An agent blocked on a dialog: read what its herdr pane shows, as data only.

The pane text is written by the agent (or whatever it ran), so it is untrusted.
It is read with the read-only `herdr pane read`, stripped of control characters,
capped in length, and classified into one label from a fixed set. The label is
the only thing that drives behaviour. Nothing here sends keys to a pane or
answers a prompt, and the text is never used as a path, URL, header or action.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import unicodedata

READ_TIMEOUT = 10        # seconds herdr gets to print the pane
READ_LINES = 40          # lines of recent pane output asked for
READ_MAX_BYTES = 64 * 1024
KEEP_LINES = 8           # non-empty lines kept
KEEP_CHARS = 600         # characters kept, cut from the front
PING_CHARS = 300

# Fixed label set. Known dialogs, matched on whitespace-normalised, lower-case text.
KNOWN = (
    ("auto_mode_setup", ("teach auto mode about your environment?",)),
    ("read_outside_dirs", ("read outside the working directories",
                           "allow this read outside the working directories?")),
)
LABELS = ("auto_mode_setup", "read_outside_dirs", "generic_menu", "unknown", "unreadable")
_MENU_LINE = re.compile(r"^\W{0,3}\s*\d{1,2}\s*[.)]\s+\S")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_DROP = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}


def sanitize(text: str) -> str:
    """Printable text only: ANSI sequences and control, format, surrogate, private-use
    and line/paragraph-separator characters gone (newline kept), blank lines collapsed,
    the last KEEP_LINES lines, at most KEEP_CHARS characters (cut from the front)."""
    text = _ANSI.sub("", text if isinstance(text, str) else "")
    text = text.replace("\r\n", "\n")
    text = "".join(c for c in text if c == "\n" or unicodedata.category(c) not in _DROP)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out = "\n".join([ln for ln in lines if ln][-KEEP_LINES:])
    if len(out) > KEEP_CHARS:
        out = "…" + out[-(KEEP_CHARS - 1):]
    return out


def classify(sanitized: str) -> str:
    """One label from LABELS. `sanitized` is the output of sanitize(); '' means the
    pane could not be read."""
    if not sanitized:
        return "unreadable"
    flat = " ".join(sanitized.lower().split())
    for label, needles in KNOWN:
        if any(n in flat for n in needles):
            return label
    if sum(1 for ln in sanitized.split("\n") if _MENU_LINE.match(ln.replace("❯", " ").replace(">", " "))) >= 2:
        return "generic_menu"
    return "unknown"


def read_pane(pane_id) -> str:
    """The recent text of one pane via `herdr pane read` (read-only), or '' on any
    failure. `pane_id` comes from the herdr agent record of the ticket's own agent."""
    if not isinstance(pane_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", pane_id):
        return ""
    try:
        cmd = shlex.split(os.environ.get("FLEET_HERDR", "herdr")) + [
            "pane", "read", pane_id, "--source", "recent", "--lines", str(READ_LINES), "--format", "text"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        timer = threading.Timer(READ_TIMEOUT, proc.kill)
        timer.start()
        try:
            data = proc.stdout.read(READ_MAX_BYTES + 1)   # bounded: never buffers more than the cap
            if len(data) > READ_MAX_BYTES:
                proc.kill()   # more than the cap: the rest is not wanted
            proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
        if len(data) > READ_MAX_BYTES:
            data = data[:READ_MAX_BYTES]
        elif proc.returncode != 0:
            return ""   # also when the timer killed it
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return data.decode("utf-8", errors="replace")


def inspect(pane_id) -> tuple[str, str]:
    """(label, sanitized text) for a blocked agent's pane."""
    text = sanitize(read_pane(pane_id))
    return classify(text), text


def one_line(sanitized: str, limit: int = PING_CHARS) -> str:
    flat = " | ".join(sanitized.split("\n"))
    return flat if len(flat) <= limit else "…" + flat[-(limit - 1):]
