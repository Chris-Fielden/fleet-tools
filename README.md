# fleet-tools

Tools for the agent fleet: tickets and the dispatcher.

## Running the fleet

- `fleet start`: starts herdr headless (`herdr server`) if needed, makes sure the `fleet` and `fleet-tools` workspaces exist, and runs `dispatch --watch` in a `dispatch` tab of `fleet-tools`. Safe to repeat.
- `fleet stop`: stops `dispatch --watch` cleanly (like Ctrl+C); agents keep running. `fleet restart` does both.
- `fleet status`: herdr, the dispatcher, Kaneo, ntfy, tailscale serve entries and autostart. No secrets are printed.
- `fleet logs [-n LINES]`: the tail of the dispatcher log and the other `$FLEET_HOME/*.log` files.
- `fleet autostart on|off`: installs or removes a systemd `--user` service that runs `fleet start` at boot (default off). Starting without a login needs linger; it prints the one `sudo loginctl enable-linger` command for you to run.
- `fleet launcher on|off`: installs or removes a desktop launcher (`~/.local/share/applications` and `~/Desktop`) that runs `fleet start` in a terminal.
