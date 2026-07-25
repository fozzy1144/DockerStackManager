"""A library of example Compose configurations, each with customization notes.

This is the reference material behind the editor's snippet browser, and the
source that ``docs/compose-reference.md`` is generated from (see
``tools/gen_compose_docs.py``) — so the examples in the docs and the examples in
the app can never drift apart.

Every :class:`Snippet` carries three things: a body to insert, a one-line
summary, and ``details`` explaining what to change and why. The details are the
point; a snippet you paste without understanding is how a stack acquires a
``privileged: true`` nobody can justify later.

``kind`` says where a body belongs, which is what lets the editor re-indent it
to the cursor:

``service``
    A complete service block, sitting under ``services:``.
``fragment``
    Keys that go *inside* a service block.
``root``
    A top-level block, at the same level as ``services:``.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Snippet:
    """One documented example configuration."""

    title: str
    category: str
    kind: str
    summary: str
    body: str
    details: str
    docs_url: str = ""


CATEGORIES = (
    "Complete services",
    "Networking & ports",
    "Storage",
    "Health & lifecycle",
    "Resources & limits",
    "Security",
    "Environment & secrets",
    "Logging",
    "Reverse proxy",
    "Reuse & structure",
)


SNIPPETS: tuple[Snippet, ...] = (
    # ── Complete services ────────────────────────────────────────────────────
    Snippet(
        title="Minimal web service",
        category="Complete services",
        kind="service",
        summary="The smallest service worth deploying: pinned, restarting, health-checked.",
        body="""web:
  image: nginx:1.27-alpine
  container_name: web
  restart: unless-stopped
  ports:
    - "8080:80"
  volumes:
    - ./site:/usr/share/nginx/html:ro
  healthcheck:
    test: ["CMD", "wget", "-qO-", "http://localhost/"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 10s
""",
        details="""Change `8080` to the host port you want; the `80` after the colon is
the port inside the container and is fixed by the image.

`:ro` makes the mount read-only. Use it for anything the container has no
business writing to — it costs nothing and turns a container compromise into
a much smaller problem.

`1.27-alpine` pins the minor version. Prefer this over `latest`: an unpinned
tag means the next `docker compose pull` can move you across a major version
without warning. Alpine variants are smaller but use musl instead of glibc,
which occasionally matters for compiled extensions.

The healthcheck uses `wget` because it exists in the Alpine image. On a
Debian-based image use `curl -f http://localhost/` instead, and confirm the
binary is actually present — a healthcheck that cannot run reports unhealthy
forever.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/",
    ),
    Snippet(
        title="PostgreSQL with a named volume",
        category="Complete services",
        kind="service",
        summary="Database with durable storage, a real healthcheck, and no published port.",
        body="""db:
  image: postgres:16-alpine
  container_name: db
  restart: unless-stopped
  environment:
    POSTGRES_DB: ${POSTGRES_DB:-appdb}
    POSTGRES_USER: ${POSTGRES_USER:-app}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
  volumes:
    - db-data:/var/lib/postgresql/data
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER:-app}"]
    interval: 10s
    timeout: 5s
    retries: 5
    start_period: 30s
""",
        details="""Note what is *missing*: there is no `ports:` block. A database only
needs to be reachable by other services on the same Compose network, which it
is by the hostname `db`. Publishing 5432 to the host exposes it to your whole
LAN — only add it if you genuinely connect from outside.

`${POSTGRES_PASSWORD:?message}` makes Compose refuse to start when the variable
is unset, instead of silently creating a database with an empty password. The
`:-default` form supplies a fallback instead.

`$$` escapes a dollar sign so Compose passes it through to the shell rather than
interpolating it itself. Inside `CMD-SHELL` you want the *container's* shell to
expand the variable.

`db-data:` is a named volume and must also be declared at the top level (see
"Named volume" under Storage). Data lives in Docker's own storage area and
survives `docker compose down` — but **not** `down -v`.

`start_period: 30s` gives the first initialisation time to finish; failures
during that window do not count toward `retries`.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#healthcheck",
    ),
    Snippet(
        title="App that waits for its database",
        category="Complete services",
        kind="service",
        summary="Ordered startup using depends_on with a health condition.",
        body="""app:
  image: ghcr.io/example/app:1.4.2
  container_name: app
  restart: unless-stopped
  depends_on:
    db:
      condition: service_healthy
  environment:
    DATABASE_URL: postgres://app:${POSTGRES_PASSWORD}@db:5432/appdb
  ports:
    - "127.0.0.1:3000:3000"
""",
        details="""`condition: service_healthy` is the difference between "started" and
"ready". Plain `depends_on: [db]` only waits for the container to exist, so an
app that connects on boot will still race the database and crash-loop until it
happens to win.

The connection host is `db` — the service key. Compose puts every service on a
shared network where service names resolve as hostnames.

`127.0.0.1:3000:3000` binds only the loopback interface, so the port is
reachable from the host itself but not from the network. This is the right
default when a reverse proxy sits in front; drop the prefix only when you
intend to expose the service directly.

Your app should still retry its connection at runtime. `depends_on` orders
startup once — it does nothing when the database restarts later.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#depends_on",
    ),
    Snippet(
        title="Scheduled job (run and exit)",
        category="Complete services",
        kind="service",
        summary="One-shot container for backups or maintenance, not restarted.",
        body="""backup:
  image: postgres:16-alpine
  profiles: ["tools"]
  depends_on:
    db:
      condition: service_healthy
  environment:
    PGPASSWORD: ${POSTGRES_PASSWORD}
  volumes:
    - ./backups:/backups
  entrypoint: ["/bin/sh", "-c"]
  command: >
    pg_dump -h db -U app -d appdb
    -f /backups/appdb-$$(date +%Y%m%d-%H%M%S).sql
""",
        details="""`profiles: ["tools"]` keeps this out of a normal `up -d`. Run it
deliberately with `docker compose run --rm backup`, or include it with
`docker compose --profile tools up`.

Do **not** give a one-shot job `restart: unless-stopped` — it will run forever
in a loop. If you want a schedule, drive it from the host's cron or a systemd
timer calling `docker compose run --rm backup`.

`>` folds the following lines into a single line, which keeps a long command
readable. Use `|` instead when newlines must be preserved.

`$$(date ...)` escapes the dollar so the container's shell runs `date`, not
Compose. Overriding `entrypoint` is necessary here because the postgres image
has its own.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#profiles",
    ),
    # ── Networking & ports ───────────────────────────────────────────────────
    Snippet(
        title="Port publishing variants",
        category="Networking & ports",
        kind="fragment",
        summary="Every form of the ports mapping, and what each one exposes.",
        body="""ports:
  - "8080:80"                 # host 8080 -> container 80, all interfaces
  - "127.0.0.1:8081:80"       # loopback only: not reachable from the network
  - "192.168.1.10:8082:80"    # one specific host interface
  - "5353:53/udp"             # UDP instead of TCP
  - "9000-9005:9000-9005"     # a contiguous range
  - target: 80                # long syntax, for when you need the extra keys
    published: "8083"
    protocol: tcp
    mode: host
""",
        details="""The rule for the short form is `[host_ip:][host_port:]container_port`.
A bare `"3000"` publishes the container port on a *random* host port, which is
rarely what you want.

**Always quote port mappings.** Unquoted `22:22` is fine, but YAML reads
`5432:5432` as a sexagesimal number in some parsers, and a mapping like
`08:80` loses its leading zero. Quoting sidesteps the whole class of problem.

Binding to `127.0.0.1` is the single most effective hardening step for a
homelab stack: services stay reachable through a reverse proxy on the same host
while being invisible to the rest of the LAN.

`mode: host` bypasses the routing mesh (only meaningful in Swarm). For plain
Compose, leave it out.

Publishing a port is not the same as `expose`. Ports are only needed for access
*from outside* the Compose network; services already reach each other on their
container ports.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#ports",
    ),
    Snippet(
        title="Custom network with aliases",
        category="Networking & ports",
        kind="root",
        summary="Separate front-end and back-end networks so the database is unreachable from outside.",
        body="""networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge
    internal: true
""",
        details="""Then give each service only the networks it needs:

    services:
      proxy:
        networks: [frontend]
      app:
        networks: [frontend, backend]
      db:
        networks: [backend]

`internal: true` removes the gateway from that network, so containers on it have
no route out to the internet at all. For a database this is usually exactly
right, and it is a stronger guarantee than simply not publishing a port.

Segmenting like this means a compromised front-end container cannot reach the
database's port directly — it has to go through the app.

Service names resolve on every shared network. Add `aliases` when you need an
extra name, for instance to migrate a hostname without touching config:

    services:
      db:
        networks:
          backend:
            aliases: [postgres, database]""",
        docs_url="https://docs.docker.com/reference/compose-file/networks/",
    ),
    Snippet(
        title="Join an existing external network",
        category="Networking & ports",
        kind="root",
        summary="Attach to a proxy network another stack owns.",
        body="""networks:
  proxy:
    external: true
    name: proxy
""",
        details="""`external: true` tells Compose the network already exists and must not
be created or removed with this stack. Create it once by hand:

    docker network create proxy

This is the standard way to put several independent stacks behind one reverse
proxy: every stack joins `proxy`, and the proxy reaches each service by name
without any ports being published to the host at all.

`name:` is needed because Compose otherwise prefixes network names with the
project name. Without it, this stack would look for `myproject_proxy`.

If the network does not exist, `up` fails with a clear error rather than
creating it — which is the intended safety behaviour.""",
        docs_url="https://docs.docker.com/reference/compose-file/networks/#external",
    ),
    Snippet(
        title="Host networking",
        category="Networking & ports",
        kind="fragment",
        summary="Share the host's network stack — for mDNS, DHCP, or VPN containers.",
        body="""network_mode: host
# ports: are ignored in host mode — the container binds host ports directly.
""",
        details="""In host mode the container has no network namespace of its own: it
binds ports on the host directly and `ports:` is silently ignored.

Legitimate uses are narrow: service discovery that needs broadcast or multicast
(Home Assistant discovery, Plex DLNA, Avahi/mDNS), DHCP servers, and anything
that manipulates the host's routing table.

The costs are real. You lose port remapping, you lose network isolation, and
the container can bind any port the host has free — including ones another
service expects. `network_mode: host` is also mutually exclusive with
`networks:`, so the container cannot reach other services by name any more;
use `localhost` and the real host port instead.

On Docker Desktop for Windows and macOS, host mode does not behave the way it
does on Linux. Do not rely on it for a stack you also run locally.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#network_mode",
    ),
    # ── Storage ──────────────────────────────────────────────────────────────
    Snippet(
        title="Named volume",
        category="Storage",
        kind="root",
        summary="Docker-managed storage that survives recreation.",
        body="""volumes:
  db-data:
  cache-data:
    driver: local
""",
        details="""A named volume with no options is the normal case, and the empty value
after the colon is valid YAML — Compose fills in the defaults.

Anything in a service's `volumes:` list *without* a slash is a named volume and
must appear here. `db-data:/var/lib/postgresql/data` is a named volume;
`./db-data:/var/lib/postgresql/data` is a bind mount to a directory beside the
compose file. Forgetting the `./` is a common and confusing mistake, which is
why the editor's linter flags undeclared names.

Named volumes survive `docker compose down` and `up`. They are destroyed by
`docker compose down -v` and are candidates for `docker volume prune` once no
container references them — so never keep the only copy of anything important
in one without a backup.

Data lives under `/var/lib/docker/volumes/<project>_<name>`. Because Docker
owns it, permissions usually just work, whereas a bind mount often needs the
`user:` of the container to match the directory's owner on the host.""",
        docs_url="https://docs.docker.com/reference/compose-file/volumes/",
    ),
    Snippet(
        title="Bind mounts and tmpfs",
        category="Storage",
        kind="fragment",
        summary="Mount host paths and RAM-backed scratch space.",
        body="""volumes:
  - ./config:/app/config:ro        # relative to the compose file
  - /srv/media:/media              # absolute host path
  - ~/documents:/docs:ro           # expanded by Compose, not the shell
  - type: tmpfs                    # RAM only, gone on restart
    target: /tmp
    tmpfs:
      size: 64m
""",
        details="""Relative paths resolve against the directory holding the compose file,
not your shell's working directory. That is what makes a stack portable — keep
config beside the compose file and the whole folder can be moved or copied.

Add `:ro` to everything the container should not modify. Config files, static
sites, and certificate bundles all qualify.

Bind mounts do not create the host path with sensible ownership — Docker makes
a *root-owned directory* if it does not exist, which then fails for a container
running as a non-root user. Create the directory first with the right owner.

`tmpfs` is genuinely useful for a container with `read_only: true` that still
needs somewhere to write scratch files, and for anything sensitive you would
rather never touch a disk. Size it explicitly or it can consume RAM until the
host suffers.""",
        docs_url="https://docs.docker.com/engine/storage/bind-mounts/",
    ),
    Snippet(
        title="NFS volume",
        category="Storage",
        kind="root",
        summary="Mount a NAS share as a Docker volume.",
        body="""volumes:
  media:
    driver: local
    driver_opts:
      type: nfs
      o: "addr=192.168.1.20,nfsvers=4,rw,soft,timeo=100"
      device: ":/export/media"
""",
        details="""Set `addr` to the NAS address and `device` to the exported path, with
the leading colon.

`soft` matters more than it looks: with the default `hard`, an unreachable
server leaves I/O blocked indefinitely and containers hang in an unkillable
state. `soft` with a `timeo` returns an error instead, which a well-behaved
application can handle.

The mount is performed by the *host* kernel, so the host needs NFS client
support (`nfs-common` on Debian/Ubuntu). It is not done inside the container.

Docker resolves the volume lazily on first use, so a typo here surfaces as a
container that fails to start rather than an error from `up`. Check
`docker volume inspect <project>_media` if it misbehaves.

For media libraries, prefer NFS over SMB/CIFS where you can — file locking and
permission semantics are far less surprising.""",
        docs_url="https://docs.docker.com/reference/compose-file/volumes/#driver_opts",
    ),
    # ── Health & lifecycle ───────────────────────────────────────────────────
    Snippet(
        title="Healthcheck variants",
        category="Health & lifecycle",
        kind="fragment",
        summary="HTTP, TCP, and CLI health probes with the timing that matters.",
        body="""healthcheck:
  # HTTP endpoint (curl on Debian-based images, wget on Alpine)
  test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]
  # TCP port only, no HTTP client needed:
  # test: ["CMD-SHELL", "nc -z localhost 8080 || exit 1"]
  # Database CLI:
  # test: ["CMD-SHELL", "pg_isready -U postgres || exit 1"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 40s
""",
        details="""`CMD` runs the command directly; `CMD-SHELL` runs it through `sh -c`,
which is what you need for pipes, `||`, or variable expansion.

`start_period` is the one people leave out and then fight. During it, failures
do not count toward `retries`, so a service that takes 30 seconds to warm up
does not get marked unhealthy on the way there. Set it slightly above your
worst-case cold start.

Total time to be declared unhealthy is roughly
`start_period + interval x retries`. With the values above, about 130 seconds.
Tighten `interval` for something a proxy depends on, loosen it for a background
worker — every check runs a process inside the container, so 5-second intervals
on twenty containers is real load.

The command must exist *in the image*. Slim and distroless images often have
neither `curl` nor `wget`; many modern images ship a health subcommand of their
own binary instead. Use `test: ["NONE"]` to disable a healthcheck an image
defines that you do not want.

Only a healthcheck makes `depends_on: condition: service_healthy` meaningful,
and it is what a "partial" stack status in this app reflects.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#healthcheck",
    ),
    Snippet(
        title="Restart policies",
        category="Health & lifecycle",
        kind="fragment",
        summary="The four policies, and which one you actually want.",
        body="""restart: unless-stopped
# no              - never restart (the default)
# on-failure:5    - restart only on non-zero exit, at most 5 times
# always          - restart even if you stopped it, including after a reboot
# unless-stopped  - restart unless you stopped it deliberately
""",
        details="""`unless-stopped` is the right default for a long-running service. It
comes back after a crash and after a host reboot, but respects a deliberate
`docker compose stop` — so a service you took down for maintenance stays down.

`always` differs in exactly one way, and it is annoying: a container you
stopped by hand comes back when the Docker daemon restarts.

`on-failure:N` suits jobs that should be retried a bounded number of times.
A clean exit is left alone.

`no` is correct for one-shot tasks. Combine it with `profiles:` so the job is
not started by a normal `up`.

None of these help if the *host* does not start Docker at boot — check
`systemctl is-enabled docker`.

A restarting container is not a healthy one. Docker restarts on process exit,
not on a failed healthcheck; a process that hangs while still running is never
restarted by this setting. That gap is what an external watchdog is for.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#restart",
    ),
    Snippet(
        title="Graceful shutdown",
        category="Health & lifecycle",
        kind="fragment",
        summary="Give a service time to finish, and reap zombie processes.",
        body="""init: true
stop_grace_period: 30s
stop_signal: SIGTERM
""",
        details="""`stop_grace_period` is how long Docker waits after the stop signal
before sending `SIGKILL`. The default is 10 seconds, which is not enough for a
database flushing to disk or a worker finishing a job — and a `SIGKILL`'d
database is how you find out whether your backups work.

`init: true` runs a tiny init process (PID 1) that forwards signals and reaps
orphaned children. Add it when the image's entrypoint is a shell script or an
application that does not handle `SIGTERM` itself; the symptom it fixes is a
container that ignores `stop` and always takes the full grace period.

`stop_signal` only needs changing for software that wants something specific —
nginx prefers `SIGQUIT` for a graceful drain.

If your application logs nothing on shutdown, it is probably not receiving the
signal at all. That is usually a shell-wrapped entrypoint swallowing it, and
`init: true` plus `exec "$@"` in the script is the fix.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#stop_grace_period",
    ),
    # ── Resources & limits ───────────────────────────────────────────────────
    Snippet(
        title="CPU and memory limits",
        category="Resources & limits",
        kind="fragment",
        summary="Stop one container from taking down the host.",
        body="""deploy:
  resources:
    limits:
      cpus: "1.5"
      memory: 1g
      pids: 200
    reservations:
      cpus: "0.25"
      memory: 256m
""",
        details="""`limits` is a hard ceiling; `reservations` is a soft guarantee used for
scheduling. Despite living under `deploy:`, both are honoured by plain
`docker compose up` — you do not need Swarm.

`cpus: "1.5"` means one and a half cores' worth of time, not specific cores.
Quote it: unquoted `1.5` is a float and some tooling mangles it.

A memory limit is the important one. Without it, a leaking container will
consume everything and the kernel's OOM killer picks a victim — frequently not
the guilty process. With a limit, only that container is killed. Watch for
exit code 137, which is exactly this.

`pids: 200` caps the process count and cheaply contains fork bombs.

Set limits from measurement, not guesswork: run `docker stats` under real load
and add headroom. Too tight is worse than none at all, because a container
killed at its ceiling looks like a mysterious crash.

The older top-level `mem_limit: 1g` and `cpus: 1.5` keys still work for
non-Swarm use, but `deploy.resources` is the current form.""",
        docs_url="https://docs.docker.com/reference/compose-file/deploy/#resources",
    ),
    # ── Security ─────────────────────────────────────────────────────────────
    Snippet(
        title="Hardened service",
        category="Security",
        kind="fragment",
        summary="Drop privileges, drop capabilities, read-only root filesystem.",
        body="""user: "1000:1000"
read_only: true
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
cap_add:
  - NET_BIND_SERVICE      # only if it must bind a port below 1024
tmpfs:
  - /tmp
  - /run
""",
        details="""Apply these one at a time and test between each; several will break a
container that assumes root, and you want to know which.

`user: "1000:1000"` runs as an unprivileged UID/GID. It must be able to read
its config and write its data, so bind-mounted directories need matching
ownership on the host (`chown -R 1000:1000 ./data`). Many images accept `PUID`
and `PGID` environment variables instead — prefer that when offered.

`read_only: true` makes the container's own filesystem immutable. Almost
everything then needs `tmpfs` for `/tmp`, and often `/run` or `/var/cache`.
Named volumes and bind mounts stay writable regardless.

`no-new-privileges:true` prevents a process gaining privileges through setuid
binaries. There is essentially no reason not to set it.

`cap_drop: [ALL]` then adding back only what is needed is the correct order.
`NET_BIND_SERVICE` for low ports and `CHOWN`/`SETUID`/`SETGID` for images that
drop privileges in their own entrypoint are the usual additions.

None of this substitutes for not publishing the port in the first place.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#cap_drop",
    ),
    Snippet(
        title="File-based secrets",
        category="Security",
        kind="root",
        summary="Pass credentials as files instead of environment variables.",
        body="""secrets:
  db_password:
    file: ./secrets/db_password.txt
""",
        details="""Reference it from a service:

    services:
      db:
        secrets: [db_password]
        environment:
          POSTGRES_PASSWORD_FILE: /run/secrets/db_password

The file is mounted at `/run/secrets/<name>`, read-only, on a tmpfs.

This is better than an environment variable because environment variables leak:
`docker inspect` shows them, they are inherited by child processes, and they
frequently end up in crash reports and log lines. A file is only read by the
code that opens it.

Many official images support a `*_FILE` variant of their variables
(`POSTGRES_PASSWORD_FILE`, `MYSQL_ROOT_PASSWORD_FILE`). Check the image's docs
before assuming; if it has none, you may need an entrypoint that reads the file.

Keep `./secrets/` out of version control and set `chmod 600` on the files. This
gets the value out of the compose file — a real improvement — but it is still
plaintext on the host, so it is not a substitute for a secret manager.""",
        docs_url="https://docs.docker.com/reference/compose-file/secrets/",
    ),
    # ── Environment & secrets ────────────────────────────────────────────────
    Snippet(
        title="Environment and .env interpolation",
        category="Environment & secrets",
        kind="fragment",
        summary="Both environment syntaxes, plus defaults and required variables.",
        body="""env_file:
  - .env
  - path: .env.local        # long form allows optional files
    required: false
environment:
  TZ: ${TZ:-Etc/UTC}                     # default if unset
  PUID: "1000"                           # quote numbers to keep them strings
  API_KEY: ${API_KEY:?set API_KEY in .env}   # fail fast if missing
  DEBUG: "false"
""",
        details="""There are two distinct mechanisms and confusing them causes real
trouble:

`env_file` is read by the *container* — those variables are not available for
interpolation inside the compose file itself.

`${VAR}` in the compose file is substituted by *Compose*, from your shell and
from the `.env` file sitting next to the compose file. This is why a `.env` in
the stack directory works for `${...}` but a differently-named env file does
not.

The four forms worth memorising:

- `${VAR}` — empty if unset
- `${VAR:-default}` — use default if unset **or** empty
- `${VAR-default}` — use default only if entirely unset
- `${VAR:?message}` — abort with the message if unset or empty

`:?` is underused. It turns "the stack silently came up misconfigured" into an
error before anything starts.

Quote numeric-looking values. Unquoted `PUID: 1000` is an integer, and some
images reject a non-string. Unquoted `false` is a boolean, and `"false"` is
what most applications actually expect to parse.

Later `env_file` entries and `environment:` both override earlier values, with
`environment:` winning.""",
        docs_url="https://docs.docker.com/reference/compose-file/services/#env_file",
    ),
    # ── Logging ──────────────────────────────────────────────────────────────
    Snippet(
        title="Log rotation",
        category="Logging",
        kind="fragment",
        summary="Cap container logs so they cannot fill the disk.",
        body="""logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
    compress: "true"
""",
        details="""By default Docker's json-file logs grow without limit. A chatty
container will eventually fill `/var/lib/docker` and take the host down with
it — this is one of the most common self-hosting outages, and it is entirely
preventable with four lines.

The settings above cap this service at roughly 30 MB. Both values must be
strings.

Prefer setting it once for the whole host in `/etc/docker/daemon.json` so new
containers inherit it:

    {
      "log-driver": "json-file",
      "log-opts": { "max-size": "10m", "max-file": "3" }
    }

then use the per-service block only for exceptions. Note that daemon defaults
apply to newly created containers, not existing ones.

`docker compose logs` only works with `json-file` and `local`. Switching to
`syslog` or `journald` sends logs elsewhere and this app's log viewer will show
nothing — that is expected, not a bug.

Check current usage with `du -sh /var/lib/docker/containers/*/*-json.log`.""",
        docs_url="https://docs.docker.com/engine/logging/configure/",
    ),
    # ── Reverse proxy ────────────────────────────────────────────────────────
    Snippet(
        title="Traefik labels",
        category="Reverse proxy",
        kind="fragment",
        summary="Expose a service through Traefik with automatic TLS.",
        body="""labels:
  - "traefik.enable=true"
  - "traefik.http.routers.myapp.rule=Host(`app.example.com`)"
  - "traefik.http.routers.myapp.entrypoints=websecure"
  - "traefik.http.routers.myapp.tls.certresolver=letsencrypt"
  - "traefik.http.services.myapp.loadbalancer.server.port=3000"
networks:
  - proxy
""",
        details="""Replace `myapp` throughout with a unique name per router — reusing one
across services silently overwrites the earlier definition. Set the `Host()`
rule to your domain, and `server.port` to the port the app listens on *inside*
the container.

`server.port` is required whenever the image exposes more than one port, and is
the usual cause of a 502 from Traefik: the router matches, but it is forwarding
to the wrong port.

The service does **not** need a `ports:` block. Traefik reaches it over the
shared `proxy` network, which is precisely the benefit — nothing is published on
the host at all. Both containers must be on that network for it to work.

`entrypoints` and `certresolver` names must match your Traefik configuration;
`websecure` and `letsencrypt` are conventional but not automatic.

Traefik reads these labels from the Docker socket, so it needs
`/var/run/docker.sock:/var/run/docker.sock:ro`. Be aware that read access to
the socket is effectively root on the host — consider a socket proxy that
exposes only the container-list endpoints.

For Nginx Proxy Manager instead, skip labels entirely: put both on the shared
network and point a proxy host at `service-name:3000` in its UI.""",
        docs_url="https://doc.traefik.io/traefik/routing/providers/docker/",
    ),
    # ── Reuse & structure ────────────────────────────────────────────────────
    Snippet(
        title="YAML anchors for shared settings",
        category="Reuse & structure",
        kind="root",
        summary="Define common service settings once and merge them everywhere.",
        body="""x-common: &common
  restart: unless-stopped
  environment:
    TZ: ${TZ:-Etc/UTC}
    PUID: "1000"
    PGID: "1000"
  logging:
    driver: json-file
    options:
      max-size: "10m"
      max-file: "3"
""",
        details="""Use it in each service with the merge key `<<`:

    services:
      sonarr:
        <<: *common
        image: linuxserver/sonarr:4.0.10
      radarr:
        <<: *common
        image: linuxserver/radarr:5.11.0

`&common` defines an anchor, `*common` references it, and `<<:` merges the
mapping into the current one. Keys you write alongside `<<:` win, so any
service can override a shared default.

Top-level keys beginning `x-` are extension fields: Compose ignores them, which
makes them the correct place to park a template. Without the `x-` prefix,
Compose would reject the unknown top-level key.

The important limitation: merging is **shallow**. A service that defines its own
`environment:` replaces the shared one entirely rather than adding to it. For
per-service extras, keep the shared values in an `env_file` instead, which does
combine.

This is the highest-value structural change for a media stack or anything else
with a dozen similar services — one place to change the timezone, one place to
fix log rotation.

Anchors must be defined before they are referenced, and they do not cross
files. `docker compose config` prints the fully resolved result, which is the
quickest way to confirm a merge did what you expected.""",
        docs_url="https://docs.docker.com/reference/compose-file/extension/",
    ),
    Snippet(
        title="Override files",
        category="Reuse & structure",
        kind="root",
        summary="Layer environment-specific changes without editing the base file.",
        body="""# docker-compose.override.yml — applied automatically on top of the base
services:
  app:
    environment:
      DEBUG: "true"
    ports:
      - "3000:3000"
    volumes:
      - ./src:/app/src
""",
        details="""`docker compose up` reads `docker-compose.yml` and, if present,
`docker-compose.override.yml`, merging the second over the first. Keep the base
file production-shaped and put local conveniences in the override.

For named files, pass them in order — later wins:

    docker compose -f docker-compose.yml -f compose.prod.yml up -d

Merge rules are worth knowing because they are not uniform: mappings
(`environment`, `labels`) merge key by key, while most sequences (`ports`,
`volumes`) are *appended* rather than replaced. That means an override adding a
port gives you both, which is occasionally surprising.

`docker compose config` shows the merged result. Use it before deploying
anything non-trivial — it resolves overrides, anchors, and `${...}`
interpolation in one go, and it is exactly what this editor's remote validation
runs.

Note that this app edits one file at a time. A stack relying on overrides is
still validated correctly, because validation runs `docker compose config` in
the stack directory where the other files live.""",
        docs_url="https://docs.docker.com/reference/compose-file/merge/",
    ),
    Snippet(
        title="Watchtower auto-updates",
        category="Reuse & structure",
        kind="service",
        summary="Automatic image updates, with the caveats spelled out.",
        body="""watchtower:
  image: containrrr/watchtower:1.7.1
  container_name: watchtower
  restart: unless-stopped
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
  environment:
    WATCHTOWER_CLEANUP: "true"
    WATCHTOWER_INCLUDE_RESTARTING: "true"
    WATCHTOWER_LABEL_ENABLE: "true"
    WATCHTOWER_SCHEDULE: "0 0 4 * * *"
    TZ: ${TZ:-Etc/UTC}
""",
        details="""Opt services in individually with a label, which is what
`WATCHTOWER_LABEL_ENABLE` requires:

    labels:
      - "com.centurylinklabs.watchtower.enable=true"

Whitelisting like this is much safer than the default of updating everything.

Understand the trade-off before enabling it. Watchtower pulls whatever the tag
now points at and recreates the container. On a pinned tag it does nothing
useful; on `latest` it can apply a breaking major upgrade at 4am with no
migration and no rollback. It pairs badly with databases in particular.

`WATCHTOWER_CLEANUP: "true"` removes the superseded images, which is worth
having — otherwise disk usage grows steadily.

The schedule is a six-field cron (seconds first); the example is 04:00 daily.

Mounting the Docker socket grants effective root on the host to this container.
That is inherent to what it does, not a misconfiguration — but it is a good
reason to pin its version, as above.

Consider whether you want this at all when you have this application: a manual
"Check All Updates" followed by a deliberate update gives you the same currency
with a human deciding when, and a working stack to roll back to.""",
        docs_url="https://containrrr.dev/watchtower/arguments/",
    ),
)


def by_category() -> dict[str, list[Snippet]]:
    """Snippets grouped by category, in :data:`CATEGORIES` order."""
    grouped: dict[str, list[Snippet]] = {name: [] for name in CATEGORIES}
    for snippet in SNIPPETS:
        grouped.setdefault(snippet.category, []).append(snippet)
    return {name: items for name, items in grouped.items() if items}


def find(title: str) -> Snippet | None:
    """Look up a snippet by its exact title."""
    for snippet in SNIPPETS:
        if snippet.title == title:
            return snippet
    return None


def reindent(body: str, indent: int) -> str:
    """Re-indent a snippet body to sit at ``indent`` spaces.

    Blank lines are left empty rather than filled with trailing whitespace,
    which keeps the inserted text clean in a diff.
    """
    prefix = " " * indent
    return "".join(
        f"{prefix}{line}\n" if line.strip() else "\n"
        for line in body.rstrip("\n").splitlines()
    )
