# Docker Stack Manager

A desktop GUI for managing and updating Linux hosts and Docker Compose stacks over SSH.

![Python](https://img.shields.io/badge/python-3.12+-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Multi-host management** — add as many Linux hosts as you need; switch between them with one click
- **Secure credentials** — passwords stored in the OS credential manager (Windows Credential Manager); never written to disk in plaintext
- **SSH key support** — connect with a private key file instead of (or alongside) a password
- **OS detection** — automatically identifies the Linux distribution on connect; colour-coded badge per distro
- **Docker stack discovery** — scans the remote host for all `docker-compose.yml` / `compose.yml` files across common paths
- **Selective stack updates** — checkboxes let you pick exactly which stacks to pull and recreate
- **System package upgrades** — runs the correct package manager (`apt`, `dnf`, `pacman`, `apk`, `zypper`) for the detected OS
- **Live log output** — streaming output panel shows real-time progress during updates

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

## Credential storage

Passwords are stored in the **OS credential manager** (Windows Credential Manager on Windows). Host configuration (hostnames, usernames, ports, key paths) is saved to `~/.docker_stack_manager/hosts.json`. No credentials are ever written to that file or to the project directory.

## Supported Linux distributions

| Distro family | Package manager |
| --- | --- |
| Ubuntu, Debian, Mint, Pop!\_OS | `apt-get` |
| Fedora, RHEL, CentOS, Rocky, AlmaLinux | `dnf` / `yum` |
| Arch, Manjaro, EndeavourOS | `pacman` |
| Alpine | `apk` |
| openSUSE | `zypper` |

## Project structure

```text
DockerStackManager/
├── main.py               # Entry point
├── run.bat               # Windows launcher
├── requirements.txt
├── models/
│   └── host.py           # Host and DockerStack data models
├── core/
│   ├── credentials.py    # Secure credential storage
│   └── ssh_client.py     # SSH connection, OS detection, stack ops
└── gui/
    ├── app.py            # Main application window
    ├── host_dialog.py    # Add/Edit host dialog
    └── log_panel.py      # Scrollable output log
```

## License

MIT
