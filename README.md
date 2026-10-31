# AppHost for Ervisio — alpha 0.1.0

**AppHost** is a self-hosted process manager inside Ervisio: bots, backends, APIs, custom workers. A native Ervisio page talks through its sandboxed SDK to a local Unix socket. A Python control runtime manages dedicated systemd template services so apps survive browser closure and reboot.

## Included

- Create from **Node.js HTTP API**, **Python HTTP API**, **Discord bot**, **Telegram bot**, or **Custom process** templates.
- Clone/update public **HTTPS Git repositories**, run an install command as the unprivileged application user, edit startup command and port.
- **Start / stop / restart**, enable / disable boot startup, and automatic on-failure restart by systemd.
- Interactive **app-scoped shell over a PTY** (separate from the running app stdin), plus journal logs (2.5-second refresh); per-app **CPU / RAM / PID / number of tasks** from systemd cgroups (3-second refresh).
- Edit text files in the app's working directory; configure environment variables (including bot tokens), and execute one-off shell commands as the app service user.
- Dark UI designed for Ervisio, without external React dependencies.

## Installation (Linux with systemd)

1. Install Ervisio version 0.5+ and enable **Plugins → Developer**. Load the `plugin/` directory (which contains `manifest.json` and `index.js`).
2. Install the companion service (privileged; inspect it before running):

   ```bash
   sudo sh plugin/runtime/install.sh
   ```

   When installed from Ervisio, the script is normally found under `/var/lib/ervisio/plugins/apphost/runtime/install.sh`.
3. Open **AppHost** in the Ervisio sidebar, unlock administrator rights, and create an app.
4. For Telegram/Discord bots, set `TELEGRAM_TOKEN`/`DISCORD_TOKEN` under **Variables**, press **Deploy** (installs Python dependencies), and then **Start**. For a Node/Python HTTP API, choose a port and start directly if its runtime is available.

**Host prerequisites:** Python 3.10+, systemd, git and `runuser` for deployments, plus Node.js/Python with venv for the relevant app templates. The HTTP API templates bind to `127.0.0.1` by default; use your existing Caddy plugin to publish an API explicitly when needed. Set `BIND_HOST=0.0.0.0` only if you intend to expose a port and have configured appropriate firewall rules.

## Architecture

```text
Ervisio plugin iframe / UI
    └── sdk.api.http("apphost") [admin, manifest allowlist]
         └── /run/ervisio-apphost/control.sock (root-only)
              └── apphost.py root control service
                   ├── systemctl ervisio-apphost@<id>.service
                   │    └── app_runner.py → workload (ervisio-apphost user)
                   ├── journalctl / systemd metrics
                   └── /srv/ervisio/apphost/<id>/{work,config.json,secrets.json}
```

## Safety and limitations

- **Admin-only** UI and API. The runtime controls systemd and therefore runs as root, but starts application commands using an unprivileged service user. Inspect startup and installer scripts before installing.
- This first version uses **one shared `ervisio-apphost` Linux user for all hosted apps**. Apps can access each other's files and environment if they are malicious. **Not safe for untrusted customers / multi-tenant hosting.** Future versions need per-app user isolation or OCI containers.
- App processes are persistent systemd services, **not attached stdin PTY sessions**. The Console is journal output plus a separate one-off command runner; the Terminal tab opens an interactive shell PTY **in the app working directory**, not an attachment to a running bot or application stdin. Full-screen ncurses apps may display imperfectly in the current basic terminal viewer.
- Resource accounting requires systemd cgroups. No per-app CPU/RAM quota UI yet. Node/Python interpreters and third-party dependencies must exist on the host; the Deploy command can install app-level dependencies.
- HTTPS Git URLs are allowed, without embedded credentials. Untrusted source code still executes with network access as the app user. Git and install operations can take several minutes; deployment jobs are in-memory and reset when the control runtime restarts.
- Secrets are not listed in app overviews, but are kept in an app-owned file and are visible to admins in Variables. Do not share the admin account.
- Workloads listen on the configured port only if their own application honors `PORT` and binds appropriately. No built-in SSL, DNS, domain, remote-node or web terminal support in this release.

## Development / tests

`plugin/index.js` is a self-contained JavaScript ES module using only `sdk.react`; no build step is needed. Test with `python3 -m unittest discover -s tests -v` and `node --check plugin/index.js`.

## Publishing

Package `plugin/` as `apphost/` at the archive root for a release, update version in `manifest.json`, and submit the repository and permissions to `Ervisio/plugins` for signature and marketplace review. The packaged ZIP in this deliverable is for developer installation, **not a signed marketplace release**.
