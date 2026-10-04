#!/usr/bin/env python3
# <xbar.title>Fleet status</xbar.title>
# <xbar.desc>Usage and fleet status from the fleet endpoint. Display only.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
"""SwiftBar plugin: refreshes every 30 seconds (the number in the file name)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_summary as fs  # noqa: E402


def render(data, now=None):
    title = fs.summarize(data, now)
    if title == fs.OFFLINE:
        return [title + " | color=red"]
    href = data.get("kaneo_url")
    link = f" href={href}" if isinstance(href, str) and href.startswith(("http://", "https://")) else ""
    lines = [title + link, "---"]
    lines += [line + link for line in fs.details(data)]
    return lines


if __name__ == "__main__":
    print("\n".join(render(fs.fetch())))
