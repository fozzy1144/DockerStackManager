# Docker Stack Manager

A desktop GUI for managing and updating Linux hosts and Docker Compose stacks over SSH.

![Python](https://img.shields.io/badge/python-3.12+-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Multi-host management** — add as many Linux hosts as you need; switch between them with one click
- **Secure credentials** — passwords stored in the OS credential manager (Windows Credential Manager); never written to disk in plaintext
- **SSH key support** — connect with a private key file instead of (or alongside) a password
- **Host key verification** — a host's key is remembered the first time you connect, and a later *change* is reported instead of silently accepted
- **OS detection** — identifies the distribution on connect, falling back to `ID_LIKE` so derivatives work too; colour-coded badge per distro
- **Docker stack discovery** — combines what Docker itself reports with a pruned filesystem scan, so it finds both running projects and stacks that have never been started
- **Selective stack updates** — checkboxes let you pick exactly which stacks to pull and recreate
- **System package upgrades** — runs the right package manager (`apt`, `dnf`, `pacman`, `apk`, `zypper`) for the detected OS
- **Works as root or not** — detects passwordless `sudo` and whether your user can reach the Docker socket, and escalates only when it has to
- **Bulk operations** — check for updates or run a full system update across every host at once, several hosts in parallel
- **Live log output** — stack pulls and package upgrades stream line by line as they happen, and can be saved to a file
- **Compose editor** — edit a stack's compose file in place with YAML highlighting, live checks, an example library, a diff before saving, host-side validation, and an automatic timestamped backup
- **Per-stack control** — container list with health and published ports, lifecycle buttons (up, restart, stop, pull, recreate, down), and log following per service
- **Disk housekeeping** — `docker system df` at a glance, with selective pruning of images, containers, build cache, and volumes
- **Fast failure when a host is down** — an unreachable host is reported in seconds with the reason, and the window stays usable while a connection is pending

## Screenshots

> Connect, scan, and update — all from one window.

![App screenshot](docs/Screenshot.png)

## Requirements

- Python 3.12+
- A Linux host reachable over SSH

## Installation

```bash
git clone https://github.com/fozzy1144/DockerStackManager.git
cd DockerStackManager
pip install -r requirements.txt
```

## Running

**Windows:**

```bat
run.bat
```

**Any platform:**

```bash
python main.py
```

## Usage

1. Click **+ Add Host** and enter the hostname/IP, port, username, and either a password or an SSH key file.
2. Select the host from the sidebar and click **Connect**.
3. Click **Scan Stacks** to discover all Docker Compose projects on the host.
4. Check or uncheck stacks, then click **Update Selected Stacks** to pull new images and recreate containers.
5. Optionally click **System Update** to run a full OS package upgrade.

Or skip straight to **Check All Updates** / **Update All Hosts** in the sidebar to work across every configured host at once.

### Managing one stack

Each discovered stack has two buttons.

**Manage** opens a window with the stack's containers — service name, state, health, image, and published ports — plus lifecycle actions and log following:

| Action | Command | Notes |
| --- | --- | --- |
| Up | `up -d` | Creates or updates containers to match the file. |
| Restart | `restart` | Restarts in place; does not re-read the compose file. |
| Stop | `stop` | Containers stay defined and can be started again. |
| Pull | `pull` | Fetches images without touching what is running. |
| Recreate | `up -d --force-recreate` | Rebuilds containers even if nothing changed. |
| Down | `down` | Removes containers and the network. **Named volumes are kept.** |

Logs stream with a tail length, an optional service filter, and follow on or off. **Stop** terminates the remote `logs -f` rather than abandoning it.

**Edit** opens the compose editor — see below.

### Editing a compose file

The editor reads the file over SFTP (falling back to `sudo cat` for root-owned files), and on save writes it back through a temp file copied over the original, so the destination keeps its owner and permissions. A timestamped backup is left beside it as `docker-compose.yml.bak.20260725-143000`.

Three things happen before anything is written:

1. **Live checks** as you type — offline, instant. They flag the mistakes that only surface after a restart: an unpinned tag, a missing restart policy, a volume referenced but never declared, two services fighting over one host port, a password sitting in plaintext. Click a finding to jump to the line.
2. **`docker compose config` on the host**, which resolves `.env` interpolation, override files, and anchors exactly as the deploy will. It runs against the real stack directory, so what it validates is what will run.
3. **Diff** shows precisely what will change.

Neither check blocks a save you insist on — each asks first.

**Examples** on the left holds documented service configurations. Pick one to read what it does and what to customise, then insert it at the cursor with the right indentation. The same material is in [docs/compose-reference.md](docs/compose-reference.md), generated from the identical source so the two cannot drift.

Shortcuts: `Ctrl+S` save, `Ctrl+F` find/replace, `F5` validate, `Tab`/`Shift+Tab` indent or outdent the selection, `Ctrl+Z`/`Ctrl+Y` undo and redo.

### What an update actually does

Per selected stack, from the stack's own directory:

```bash
docker compose pull      # if this fails, the stack is left alone
docker compose up -d
```

A failed pull deliberately aborts before `up`, because recreating containers against half-fetched images is how a routine update becomes an outage. Stack statuses are re-scanned when the run finishes.

## Where things are stored

| What | Where |
| --- | --- |
| Passwords and key passphrases | OS credential manager, under the service `DockerStackManager` |
| Hosts, ports, key paths, discovered stacks | `~/.docker_stack_manager/hosts.json` |
| Accepted SSH host keys | `~/.docker_stack_manager/known_hosts` |

No credentials are ever written to `hosts.json` or to the project directory. The config file is written atomically — an interrupted save leaves the previous version intact rather than a truncated file — and a single malformed record is skipped at startup instead of stopping the app.

## Supported Linux distributions

| Distro family | Package manager |
| --- | --- |
| Debian, Ubuntu, Mint, Pop!\_OS, Raspbian, Kali, Devuan, elementary | `apt-get` |
| Fedora, RHEL, CentOS, Rocky, AlmaLinux, Oracle, Amazon Linux | `dnf` / `yum` |
| Arch, Manjaro, EndeavourOS, Garuda | `pacman` |
| Alpine | `apk` |
| openSUSE, SLES | `zypper` |

Anything else falls back to its `ID_LIKE` family, and finally to `apt-get`. Unrecognised distros are called out in the log so the guess is visible rather than silent.

## Privileges

System updates need root. The app works this out per host, in order:

1. Logged in as `root` — commands run directly.
2. Passwordless `sudo` available — uses `sudo -n`, and your stored password never goes over the wire.
3. Otherwise — the stored password is fed to `sudo -S` on stdin.

Docker commands follow the same idea: if your user cannot reach the Docker socket (not in the `docker` group), stack operations are escalated automatically.

> **Key-only hosts:** if a host authenticates with an SSH key and has no stored password, there is nothing to give `sudo`. Either configure passwordless `sudo` for that user or log in as `root`. The log says so explicitly rather than hanging on an invisible prompt.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| *"the host appears to be offline"* | No TCP answer within the probe budget. The device is off, asleep, or firewalled. |
| *"refused the connection"* | The host is up but nothing is listening on that port — check `sshd` and the port number. |
| *"Cannot resolve …"* | DNS, or a typo in the hostname. Try the IP address. |
| *"Host key … has changed"* | The server was rebuilt, or the connection is being intercepted. Verify, then remove the stale line from `~/.docker_stack_manager/known_hosts`. |
| *"Authentication failed"* | Wrong password, or a key whose passphrase is not the stored one. |
| System update fails immediately | No route to root — see [Privileges](#privileges). |
| No stacks found | Compose files live outside the scanned roots (`/opt`, `/srv`, `/home`, `/root`, `/docker`, `/stacks`, `/data`). A full-tree scan runs automatically if the first pass finds nothing. |
| Update count looks stale | Counts come from the package lists already on the host; refreshing them needs root. Run a system update to resynchronise. |
| Editor cannot save | The compose file needs root and passwordless `sudo` is not configured — see [Privileges](#privileges). The backup and the write happen together, so a failure changes nothing. |
| Log viewer shows nothing | The stack's logging driver is not `json-file` or `local`; `docker compose logs` cannot read `syslog` or `journald`. |

## Architecture

```text
DockerStackManager/
├── main.py                  # Entry point
├── run.bat                  # Windows launcher
├── requirements.txt
├── models/
│   └── host.py              # Host, DockerStack, Container
├── core/
│   ├── distro.py            # Per-distro package-manager commands and badge colours
│   ├── credentials.py       # Keyring secrets + atomic config persistence
│   ├── compose.py           # Compose parsing, linting, diffing (no network)
│   ├── snippets.py          # Documented example configurations
│   └── ssh_client.py        # SSH transport: connect, probe, run, stream, transfer
├── gui/
│   ├── app.py               # Main window and action coordination
│   ├── theme.py             # Shared severity and state colours
│   ├── host_list.py         # Sidebar host cards
│   ├── host_dialog.py       # Add/Edit host dialog
│   ├── log_panel.py         # Batched, thread-safe output log
│   ├── code_editor.py       # YAML editor widget: gutter, highlighting, find
│   ├── compose_editor.py    # Editor window: examples, checks, diff, save
│   ├── stack_window.py      # Containers, lifecycle actions, log following
│   └── maintenance.py       # Disk usage and pruning
├── tools/
│   └── gen_compose_docs.py  # Regenerates docs/compose-reference.md
└── tests/                   # stdlib unittest; no dependencies required
```

Run the tests with:

```bash
python -m unittest discover -s tests -t .
```

Four conventions hold the layers apart and are worth preserving:

**Distro knowledge lives only in `core/distro.py`.** `ssh_client` knows how to *run* a command, never *which* command; it is handed a `PackageManager` and executes its strings. Supporting a new distro is a one-line table entry, not a new branch in an `if`/`elif` chain.

**`core/` never imports from `gui/`.** Everything in `core/` is testable without a display, which is why the compose linter, the snippet library, the output pump, and the reachability probe all have real tests.

**Nothing blocks the UI thread.** Every network call runs on a worker thread and returns via `after()`. The one exception is logging: `LogPanel.log` is safe to call from any thread because it only enqueues, and a single timer drains the queue with one batched widget insert per tick — which is what keeps the window responsive while `apt` streams thousands of lines.

**A pending connection must not lock the window.** Connecting deliberately does *not* set the busy flag, and the attempt is tracked by a token so switching hosts mid-connect abandons it. An apparently frozen window while a dead host timed out was a real bug; the reachability probe and this token are the two halves of the fix.

## Documentation

- [docs/compose-reference.md](docs/compose-reference.md) — every example configuration with customization notes, plus the full list of checks the editor runs. Generated from `core/snippets.py`; run `python tools/gen_compose_docs.py` after editing the library.

## License

MIT
