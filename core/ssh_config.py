"""Read hosts out of an OpenSSH ``config`` file.

This is where VS Code's Remote-SSH targets come from: the extension has no host
list of its own, it reads ``~/.ssh/config`` (or whatever
``remote.SSH.configFile`` points at). Parsing that file therefore imports exactly
the set of machines you already connect to from the editor.

Only what this application needs is interpreted — ``HostName``, ``User``,
``Port``, ``IdentityFile``, ``ProxyJump`` — but the parts of OpenSSH's semantics
that change the *answer* are honoured:

* keywords are case-insensitive, and accept ``key value`` or ``key=value``
* a ``Host`` line may carry several patterns, including wildcards
* **first obtained value wins** — a later block cannot override an earlier one,
  which is why ``Host *`` defaults belong at the bottom of a config file
* ``Include`` pulls in other files, relative to the including file's directory

``Match`` blocks are deliberately skipped: their conditions depend on the
connection being attempted, so importing from them would be guesswork.
"""

import fnmatch
import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: Keywords worth reading. Everything else is ignored.
_WANTED = ("hostname", "user", "port", "identityfile", "proxyjump")

_MAX_INCLUDE_DEPTH = 8
"""Guard against an Include cycle, which OpenSSH itself also refuses."""


@dataclass(frozen=True, slots=True)
class SSHConfigHost:
    """One importable host, as resolved from a config file."""

    alias: str
    """The name on the ``Host`` line — what you type after ``ssh``."""

    hostname: str
    """Resolved ``HostName``; falls back to the alias, as OpenSSH does."""

    user: str = ""
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = ""
    source: str = ""
    """Which config file this came from, for display."""

    @property
    def address(self) -> str:
        base = f"{self.user}@{self.hostname}" if self.user else self.hostname
        return base if self.port == 22 else f"{base}:{self.port}"

    @property
    def is_complete(self) -> bool:
        """Whether there is enough here to attempt a connection."""
        return bool(self.hostname and self.user)


@dataclass
class _Block:
    patterns: list[str]
    settings: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""


def default_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def vscode_config_path() -> Optional[Path]:
    """The path VS Code is configured to use, if it overrides the default.

    Remote-SSH reads ``~/.ssh/config`` unless ``remote.SSH.configFile`` is set,
    so honouring that setting is what makes "the hosts I see in VS Code" and
    "the hosts imported here" the same list.
    """
    settings = _vscode_settings()
    configured = settings.get("remote.SSH.configFile")
    if not isinstance(configured, str) or not configured.strip():
        return None
    return Path(os.path.expandvars(os.path.expanduser(configured.strip())))


def _vscode_settings() -> dict:
    """Load VS Code's settings.json, tolerating its JSON-with-comments dialect."""
    for candidate in _vscode_settings_paths():
        try:
            raw = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = _loads_jsonc(raw)
        if parsed is not None:
            return parsed
    return {}


def _vscode_settings_paths() -> list[Path]:
    """Where settings.json lives, across the VS Code variants and platforms."""
    home = Path.home()
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    roots += [
        home / "Library" / "Application Support",  # macOS
        home / ".config",  # Linux
    ]
    flavours = ("Code", "Code - Insiders", "VSCodium")
    return [root / flavour / "User" / "settings.json"
            for root in roots for flavour in flavours]


def _loads_jsonc(raw: str) -> Optional[dict]:
    """Parse JSON that may carry comments and trailing commas.

    VS Code writes JSONC. Stripping comments outside string literals and then
    dropping trailing commas covers what it actually produces.
    """
    for attempt in (raw, _strip_jsonc(raw)):
        try:
            parsed = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_jsonc(raw: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if raw.startswith("//", index):
            index = raw.find("\n", index)
            if index == -1:
                break
            continue
        if raw.startswith("/*", index):
            end = raw.find("*/", index + 2)
            index = len(raw) if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def candidate_paths() -> list[Path]:
    """Config files worth reading, most authoritative first, de-duplicated."""
    paths: list[Path] = []
    for candidate in (vscode_config_path(), default_config_path()):
        if candidate is None:
            continue
        resolved = _normalise(candidate)
        if resolved not in [_normalise(p) for p in paths]:
            paths.append(candidate)
    return paths


def _normalise(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def load_hosts(paths: Optional[Iterable[Path]] = None) -> list[SSHConfigHost]:
    """Parse the given config files (or the discovered ones) into hosts.

    Aliases are de-duplicated across files, keeping the first occurrence, which
    matches OpenSSH's first-wins rule.
    """
    seen: set[str] = set()
    hosts: list[SSHConfigHost] = []
    for path in list(paths) if paths is not None else candidate_paths():
        for host in parse_file(path):
            key = host.alias.lower()
            if key in seen:
                continue
            seen.add(key)
            hosts.append(host)
    return hosts


def parse_file(path: Path) -> list[SSHConfigHost]:
    """Parse one config file, following ``Include``. Missing files yield ``[]``."""
    blocks = _read_blocks(path, depth=0)
    return _resolve(blocks)


def parse_text(text: str, source: str = "config") -> list[SSHConfigHost]:
    """Parse config content directly. ``Include`` is not followed."""
    return _resolve(_parse_blocks(text, source, base_dir=None, depth=_MAX_INCLUDE_DEPTH))


def _read_blocks(path: Path, depth: int) -> list[_Block]:
    if depth > _MAX_INCLUDE_DEPTH:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []
    return _parse_blocks(text, str(path), base_dir=path.parent, depth=depth)


def _parse_blocks(
    text: str, source: str, base_dir: Optional[Path], depth: int
) -> list[_Block]:
    blocks: list[_Block] = []
    # Settings before the first Host line are global defaults, and because
    # first-wins they take precedence over everything that follows.
    current = _Block(patterns=["*"], source=source)
    blocks.append(current)
    skipping_match = False

    for line in text.splitlines():
        keyword, value = _split_line(line)
        if keyword is None:
            continue

        if keyword == "host":
            skipping_match = False
            current = _Block(patterns=_split_patterns(value), source=source)
            blocks.append(current)
            continue

        if keyword == "match":
            # Conditional on the connection being made; not importable.
            skipping_match = True
            continue

        if skipping_match:
            continue

        if keyword == "include":
            if base_dir is not None:
                blocks.extend(_read_included(value, base_dir, depth))
            continue

        if keyword in _WANTED:
            current.settings.setdefault(keyword, []).append(value)

    return blocks


def _read_included(value: str, base_dir: Path, depth: int) -> list[_Block]:
    blocks: list[_Block] = []
    for pattern in _split_patterns(value):
        expanded = os.path.expandvars(os.path.expanduser(pattern))
        if not os.path.isabs(expanded):
            expanded = str(base_dir / expanded)
        for match in sorted(glob.glob(expanded)):
            blocks.extend(_read_blocks(Path(match), depth + 1))
    return blocks


def _split_line(line: str) -> tuple[Optional[str], str]:
    """Split a config line into a lower-cased keyword and its value."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, ""
    # Either "keyword value" or "keyword=value".
    if "=" in stripped and (
        " " not in stripped.split("=", 1)[0].strip()
        and not re.match(r"^\S+\s", stripped)
    ):
        keyword, _, value = stripped.partition("=")
    else:
        parts = re.split(r"[\s=]+", stripped, maxsplit=1)
        keyword, value = parts[0], parts[1] if len(parts) > 1 else ""
    return keyword.strip().lower(), value.strip()


def _split_patterns(value: str) -> list[str]:
    """Split a whitespace-separated pattern list, honouring quotes."""
    patterns: list[str] = []
    for token in re.findall(r'"[^"]*"|\S+', value):
        cleaned = token.strip().strip('"')
        if cleaned:
            patterns.append(cleaned)
    return patterns


def _resolve(blocks: list[_Block]) -> list[SSHConfigHost]:
    """Turn parsed blocks into concrete hosts, applying first-wins resolution."""
    hosts: list[SSHConfigHost] = []
    seen: set[str] = set()

    for block in blocks:
        for alias in block.patterns:
            if _is_pattern(alias) or alias.lower() in seen:
                continue
            seen.add(alias.lower())
            hosts.append(_resolve_alias(alias, blocks))
    return hosts


def _resolve_alias(alias: str, blocks: list[_Block]) -> SSHConfigHost:
    resolved: dict[str, str] = {}
    source = ""
    for block in blocks:
        if not _matches(alias, block.patterns):
            continue
        for keyword, values in block.settings.items():
            # First obtained value wins — never overwrite.
            if keyword not in resolved and values:
                resolved[keyword] = values[0]
        if not source and not all(_is_pattern(p) for p in block.patterns):
            source = block.source

    return SSHConfigHost(
        alias=alias,
        hostname=resolved.get("hostname") or alias,
        user=resolved.get("user", ""),
        port=_as_port(resolved.get("port")),
        identity_file=_expand_path(resolved.get("identityfile", "")),
        proxy_jump=resolved.get("proxyjump", ""),
        source=source,
    )


def _matches(alias: str, patterns: list[str]) -> bool:
    """OpenSSH pattern matching, including ``!`` negation."""
    matched = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if fnmatch.fnmatch(alias.lower(), pattern[1:].lower()):
                return False
        elif fnmatch.fnmatch(alias.lower(), pattern.lower()):
            matched = True
    return matched


def _is_pattern(value: str) -> bool:
    return any(char in value for char in "*?!")


def _as_port(value: Optional[str]) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return 22
    return port if 1 <= port <= 65535 else 22


def _expand_path(value: str) -> str:
    """Expand ``~`` and environment variables in an IdentityFile path.

    Quotes are stripped because a Windows path with spaces is usually quoted in
    the config file, and paramiko wants the bare path.
    """
    if not value:
        return ""
    cleaned = value.strip().strip('"')
    return os.path.expandvars(os.path.expanduser(cleaned))


# ──────────────────────────────────────────────────────────────────────────────
# Deciding what to import
# ──────────────────────────────────────────────────────────────────────────────

ACTION_ADD = "add"
"""Not configured here yet — create a new host."""

ACTION_ATTACH_KEY = "attach-key"
"""Already configured, but without the key the SSH config names for it."""

ACTION_SKIP = "skip"
"""Nothing to do, or not enough information to act on."""


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    """One config host, paired with what importing it would actually do."""

    host: SSHConfigHost
    action: str
    reason: str
    existing_index: int = -1
    """Index into the existing host list for :data:`ACTION_ATTACH_KEY`, else -1."""

    @property
    def actionable(self) -> bool:
        return self.action != ACTION_SKIP


def plan_import(
    config_hosts: Iterable[SSHConfigHost], existing: Iterable
) -> list[ImportCandidate]:
    """Work out what each SSH config host would change, without changing it.

    Hosts are matched on user, hostname and port together: the same machine
    reached as two different accounts is genuinely two entries here, because the
    credentials and the privileges differ.
    """
    # Materialised once. Re-listing per candidate was quadratic, and silently
    # wrong for any one-shot iterable, which the signature permits.
    current_hosts = list(existing)
    index_by_key = {
        _match_key(host.hostname, host.username, host.port): position
        for position, host in enumerate(current_hosts)
    }

    candidates: list[ImportCandidate] = []
    for config_host in config_hosts:
        if not config_host.user:
            candidates.append(
                ImportCandidate(
                    config_host,
                    ACTION_SKIP,
                    "no User in the SSH config — add this one by hand",
                )
            )
            continue

        position = index_by_key.get(
            _match_key(config_host.hostname, config_host.user, config_host.port)
        )
        if position is None:
            candidates.append(
                ImportCandidate(config_host, ACTION_ADD, "not configured yet")
            )
            continue

        current = current_hosts[position]
        if config_host.identity_file and not getattr(current, "key_path", ""):
            candidates.append(
                ImportCandidate(
                    config_host,
                    ACTION_ATTACH_KEY,
                    f"already added — would attach {Path(config_host.identity_file).name}",
                    position,
                )
            )
            continue

        candidates.append(
            ImportCandidate(config_host, ACTION_SKIP, "already configured")
        )

    return candidates


def _match_key(hostname: str, user: str, port: int) -> tuple[str, str, int]:
    return (hostname.strip().lower(), user.strip().lower(), port)
