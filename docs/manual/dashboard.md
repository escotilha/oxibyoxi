# Dashboard

oxi ships a localhost HTML dashboard for monitoring task state, budget, and recent events.

## Starting the dashboard

```bash
oxi dashboard
# oxi: dashboard at http://127.0.0.1:8765/ (Ctrl-C to stop)
```

Options:

```bash
oxi dashboard --port 9000       # custom port (default: 8765)
oxi dashboard --hours 48        # show events from last 48h (default: 24)
```

The dashboard binds to `127.0.0.1` by default and is accessible only from the local machine. This is intentional — see [Safety rails — dashboard](safety-rails.md#dashboard-is-localhost-only).

## What the dashboard shows

- **Instance name and plan tier** — top of every page
- **Budget bar** — today's spend vs. soft-warn and hard-cap thresholds; turns yellow at warn, red at hard-stop
- **Task table** — all tasks with current status, tier, PR number (linked to GitHub), and cost
- **Event log** — recent engine events in reverse-chronological order

## Widening the bind (advanced)

If you need to access the dashboard remotely, front it with a reverse proxy that handles authentication. oxi has no built-in auth. Widening the bind to a non-loopback interface exposes task titles, PR numbers, cost data, and failure reasons to anyone reachable on that interface.

Example with nginx (not an oxi feature — operator responsibility):

```nginx
server {
    listen 443 ssl;
    server_name oxi.internal.example.com;

    auth_basic "oxi dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8765;
    }
}
```

Never widen the bind on a machine accessible from the public internet without authentication.
