#!/usr/bin/env python3
"""iTerm2 status bar component (Python API): 'Fleet status'.

Add it in iTerm2 > Settings > Profiles > Session > Configure Status Bar, then
run this script from Scripts > AutoLaunch (docs/mac-widgets.md). Clicking the
component opens the Kaneo board. It reads only the fleet status endpoint.
"""
import asyncio
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_summary as fs  # noqa: E402

REFRESH = 30
_board = {"url": None}


def component_text(data, now=None):
    """The text of the component; remembers the board address for clicks."""
    text = fs.summarize(data, now)
    href = data.get("kaneo_url") if isinstance(data, dict) else None
    _board["url"] = href if text != fs.OFFLINE and isinstance(href, str) and href.startswith(("http://", "https://")) else None
    return text


async def main(connection):
    import iterm2
    component = iterm2.StatusBarComponent(
        short_description="Fleet status", detailed_description="Plan usage, agents running, tickets ready to ship",
        knobs=[], exemplar="5h 42% | 1 running | 2 to ship", update_cadence=REFRESH,
        identifier="dev.fleet.status")

    @iterm2.StatusBarRPC
    async def fleet_status(knobs):
        data = await asyncio.get_running_loop().run_in_executor(None, fs.fetch)
        return component_text(data)

    @iterm2.RPC
    async def on_click(session_id):
        if _board["url"]:
            webbrowser.open(_board["url"])

    await component.async_register(connection, fleet_status, onclick=on_click)


if __name__ == "__main__":
    import iterm2
    iterm2.run_forever(main)
