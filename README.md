# fleet-tools

Tools for the agent fleet: tickets and the dispatcher.

## One command: `fleet <verb>`

`fleet help` lists every verb. Besides the ones below, `fleet ship ID`, `fleet ticket ARGS`, `fleet approve ID` (same as `ticket approve`), `fleet alert REPO TEXT` and `fleet idea TEXT` do exactly what `ship`, `ticket`, `alert` and `idea` do; those four keep working unchanged as aliases. Agents are blocked from the human-only verbs by the guard hook under both names (`fleet ship`, `fleet approve`, `fleet ticket approve|close|sync`, `fleet alert`, `fleet idea`, `fleet start|stop|restart|autostart|launcher|ops`).

- `fleet ops`: opens or reattaches a persistent tmux session named `fleet` with two windows: `ops` (a shell for fleet commands; its prompt starts with a bright red `[LOIC ops]` so it cannot be mistaken for the Mac or an agent tab) and `herdr` (herdr attached read-only, for watching; it runs in a detached `fleet-herdr` tmux session and the window attaches with `tmux attach -r`, which ignores keys). Agents are spoken to only through ticket notes (`ticket note`), never by typing into herdr. Needs tmux; if it is missing, run `sudo apt install -y tmux` (it is not installed for you).

## Running the fleet

- `fleet start`: starts herdr headless (`herdr server`) if needed, makes sure the `fleet` and `fleet-tools` workspaces exist, and runs `dispatch --watch` in a `dispatch` tab of `fleet-tools`. Safe to repeat.
- `fleet stop`: stops `dispatch --watch` cleanly (like Ctrl+C); agents keep running. `fleet restart` does both.
- `fleet status`: herdr, the dispatcher, Kaneo, ntfy, tailscale serve entries and autostart. No secrets are printed. `fleet status --json` prints the widget snapshot instead, and `fleet status-serve` serves it for the Mac widgets (see docs/mac-widgets.md).
- `fleet logs [-n LINES]`: the tail of the dispatcher log and the other `$FLEET_HOME/*.log` files.
- `fleet autostart on|off`: installs or removes a systemd `--user` service that runs `fleet start` at boot (default off). Starting without a login needs linger; it prints the one `sudo loginctl enable-linger` command for you to run.
- `fleet launcher on|off`: installs or removes a desktop launcher (`~/.local/share/applications` and `~/Desktop`) that runs `fleet start` in a terminal.
