# systemd supervision

These user units keep the autonomous loop alive without a human babysitting it.
They are the prevention layer for the 15h silent flatline: the harness's own
watchdog runs *inside* the harness, so when the process died nothing noticed.
These run outside it.

| Unit | Role |
|------|------|
| `boardroom-api.service` | FastAPI command center on :8503, `Restart=always` |
| `boardroom-harness.service` | The agent loop, `Restart=always`. Its preflight exits non-zero if Postgres/Ollama are down, so it crash-loops (every 10s) until deps recover. On every (re)start it reaps orphaned runs and revives stranded tasks. |
| `boardroom-liveness.timer` + `.service` | Every 3 min, an *external* check reads last-activity from Postgres; if the loop is quiet past `LIVENESS_STALL_SECONDS` (default 1200) while unpaused and before the deadline, it restarts the harness and fires `ALERT_WEBHOOK_URL` if set. Catches *hangs* (process alive, doing nothing) that `Restart=always` can't. |

## Install

Unit files hard-code absolute paths (`/home/nicholas/...`); adjust if the repo
or venv moves.

```sh
cp deploy/systemd/boardroom-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now boardroom-api.service boardroom-harness.service boardroom-liveness.timer
loginctl enable-linger "$USER"   # so it survives logout / runs at boot
```

## Optional push alerts

Add to `.env` (read by the liveness unit via `EnvironmentFile`):

```sh
ALERT_WEBHOOK_URL=https://ntfy.sh/<your-private-topic>   # or a Slack/Discord webhook
LIVENESS_STALL_SECONDS=1200
LIVENESS_RESTART=1        # set 0 to alert without auto-restarting
```

## Operate

```sh
systemctl --user status boardroom-harness
journalctl --user -u boardroom-harness -f
systemctl --user restart boardroom-harness
systemctl --user start boardroom-liveness.service   # run the watchdog once, now
```
