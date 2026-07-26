"""Compose file inspection: parse, lint, and diff.

Pure logic — nothing here touches the network. The editor uses it for instant
feedback while typing, and the remote ``docker compose config`` check (see
:meth:`core.ssh_client.SSHClient.validate_compose`) is the authoritative second
opinion that also resolves ``.env`` interpolation.

The lint rules encode the mistakes that actually bite when a stack is updated
unattended: an unpinned tag that silently changes major version, a missing
restart policy that leaves a service down after a reboot, a volume that is
referenced but never declared.

PyYAML is an optional dependency. Without it the structural rules are skipped
and only the cheap textual checks run, so the editor still works — it just
leans on the remote validator instead.
"""

import difflib
import re
from dataclasses import dataclass
from typing import Any, Optional

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the install
    yaml = None  # type: ignore[assignment]
    YAML_AVAILABLE = False

ERROR = "error"
WARNING = "warning"
INFO = "info"

_LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

#: Keys that identify where a service's image comes from. One is required.
_IMAGE_KEYS = ("image", "build")

#: Environment variable names that should not hold a literal value in the file.
_SECRET_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One lint result, optionally anchored to a line in the source text."""

    level: str
    message: str
    line: int = 0
    """1-based line number, or 0 when the finding is about the file as a whole."""

    hint: str = ""
    """What to do about it, shown alongside the message in the editor."""

    @property
    def is_error(self) -> bool:
        return self.level == ERROR

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{self.level.upper()}: {where}{self.message}"


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Outcome of parsing a compose document."""

    data: Optional[dict]
    """The parsed mapping, or ``None`` when the text is not usable YAML."""

    error: Optional[Finding] = None

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error is None


def parse(text: str) -> ParseResult:
    """Parse compose YAML, converting a syntax error into a located Finding."""
    if not YAML_AVAILABLE:
        return ParseResult(data=None)
    if not text.strip():
        return ParseResult(
            data=None,
            error=Finding(ERROR, "The file is empty.", 0, "Add a 'services:' block."),
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ParseResult(data=None, error=_finding_from_yaml_error(exc))

    if data is None:
        return ParseResult(
            data=None,
            error=Finding(ERROR, "The file contains no YAML document.", 0),
        )
    if not isinstance(data, dict):
        return ParseResult(
            data=None,
            error=Finding(
                ERROR,
                f"The top level must be a mapping, not {type(data).__name__}.",
                1,
            ),
        )
    return ParseResult(data=data)


def _finding_from_yaml_error(exc: Exception) -> Finding:
    line = 0
    detail = str(exc).split("\n")[0]
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        line = mark.line + 1
        problem = getattr(exc, "problem", None) or detail
        detail = problem.strip()
    return Finding(
        ERROR,
        f"YAML syntax error: {detail}",
        line,
        "Compose files are indentation-sensitive; use spaces, never tabs.",
    )


def lint(text: str) -> list[Finding]:
    """Check a compose document and return findings, most severe first."""
    findings: list[Finding] = []
    findings.extend(_textual_findings(text))

    result = parse(text)
    if result.error is not None:
        # A syntax error makes every structural rule meaningless.
        return [result.error] + findings
    if result.data is None:
        return findings

    findings.extend(_structural_findings(text, result.data))
    findings.sort(key=lambda f: (_LEVEL_ORDER.get(f.level, 3), f.line))
    return findings


def _textual_findings(text: str) -> list[Finding]:
    """Checks that need only the raw text, so they work without PyYAML."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "\t" in line[: len(line) - len(line.lstrip("\t "))]:
            findings.append(
                Finding(
                    ERROR,
                    "Tab character used for indentation.",
                    number,
                    "YAML forbids tabs for indentation — use two spaces per level.",
                )
            )
    return findings


def _structural_findings(text: str, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    if "version" in data:
        findings.append(
            Finding(
                INFO,
                "'version' is obsolete and ignored by Compose v2.",
                _line_of_key(text, "version"),
                "Delete the line; the Compose Specification has no version field.",
            )
        )

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        findings.append(
            Finding(
                ERROR,
                "No services defined.",
                _line_of_key(text, "services"),
                "A compose file needs a 'services:' mapping with at least one entry.",
            )
        )
        return findings

    declared_volumes = set(_top_level_names(data, "volumes"))
    declared_networks = set(_top_level_names(data, "networks"))
    container_names: dict[str, str] = {}
    published: dict[str, str] = {}

    for name, service in services.items():
        line = _line_of_key(text, str(name))
        if not isinstance(service, dict):
            findings.append(
                Finding(ERROR, f"Service '{name}' is not a mapping.", line)
            )
            continue

        findings.extend(
            _service_findings(
                text,
                str(name),
                service,
                line,
                services,
                declared_volumes,
                declared_networks,
                container_names,
                published,
            )
        )

    return findings


def _service_findings(
    text: str,
    name: str,
    service: dict,
    line: int,
    all_services: dict,
    declared_volumes: set[str],
    declared_networks: set[str],
    container_names: dict[str, str],
    published: dict[str, str],
) -> list[Finding]:
    findings: list[Finding] = []

    if not any(key in service for key in _IMAGE_KEYS):
        findings.append(
            Finding(
                ERROR,
                f"Service '{name}' has neither 'image' nor 'build'.",
                line,
                "Every service needs an image to run or a build context.",
            )
        )

    image = service.get("image")
    if isinstance(image, str) and image:
        tag = _image_tag(image)
        if tag in ("", "latest"):
            findings.append(
                Finding(
                    WARNING,
                    f"Service '{name}' uses the '{tag or 'latest'}' tag.",
                    _line_of_key(text, "image", after=line),
                    "An unpinned tag can jump a major version on the next pull. "
                    "Pin a version, or a digest for exact reproducibility.",
                )
            )

    if "restart" not in service and "deploy" not in service:
        findings.append(
            Finding(
                WARNING,
                f"Service '{name}' has no restart policy.",
                line,
                "Add 'restart: unless-stopped' so it returns after a reboot or crash.",
            )
        )

    if "healthcheck" not in service:
        findings.append(
            Finding(
                INFO,
                f"Service '{name}' has no healthcheck.",
                line,
                "Without one, Compose calls the container healthy as soon as it "
                "starts, and 'depends_on: service_healthy' cannot wait for it.",
            )
        )

    if service.get("privileged") is True:
        findings.append(
            Finding(
                WARNING,
                f"Service '{name}' runs privileged.",
                _line_of_key(text, "privileged", after=line),
                "This disables container isolation. Prefer specific 'cap_add' "
                "entries or a device mapping.",
            )
        )

    container_name = service.get("container_name")
    if isinstance(container_name, str) and container_name:
        if container_name in container_names:
            findings.append(
                Finding(
                    ERROR,
                    f"container_name '{container_name}' is used by both "
                    f"'{container_names[container_name]}' and '{name}'.",
                    _line_of_key(text, "container_name", after=line),
                    "Container names must be unique on the host.",
                )
            )
        else:
            container_names[container_name] = name

    findings.extend(_port_findings(text, name, service, line, published))
    findings.extend(
        _reference_findings(
            text, name, service, line, all_services, declared_volumes, declared_networks
        )
    )
    findings.extend(_secret_findings(text, name, service, line))
    return findings


def _port_findings(
    text: str, name: str, service: dict, line: int, published: dict[str, str]
) -> list[Finding]:
    findings: list[Finding] = []
    for host_port in _published_ports(service.get("ports")):
        if host_port in published:
            findings.append(
                Finding(
                    ERROR,
                    f"Host port {host_port} is published by both "
                    f"'{published[host_port]}' and '{name}'.",
                    _line_of_key(text, "ports", after=line),
                    "Two containers cannot bind the same host port; the second "
                    "will fail to start.",
                )
            )
        else:
            published[host_port] = name
    return findings


def _reference_findings(
    text: str,
    name: str,
    service: dict,
    line: int,
    all_services: dict,
    declared_volumes: set[str],
    declared_networks: set[str],
) -> list[Finding]:
    findings: list[Finding] = []

    for volume in _named_volume_refs(service.get("volumes")):
        if volume not in declared_volumes:
            findings.append(
                Finding(
                    ERROR,
                    f"Service '{name}' mounts named volume '{volume}', "
                    f"which is not declared.",
                    _line_of_key(text, "volumes", after=line),
                    f"Add a top-level 'volumes:' entry for '{volume}'. Anything "
                    f"without a '/' is treated as a named volume, not a path.",
                )
            )

    networks = service.get("networks")
    for network in _network_refs(networks):
        if network not in declared_networks:
            findings.append(
                Finding(
                    ERROR,
                    f"Service '{name}' joins network '{network}', "
                    f"which is not declared.",
                    _line_of_key(text, "networks", after=line),
                    f"Add a top-level 'networks:' entry for '{network}', or mark "
                    f"it 'external: true' if another stack owns it.",
                )
            )

    for dependency in _depends_on_refs(service.get("depends_on")):
        if dependency not in all_services:
            findings.append(
                Finding(
                    ERROR,
                    f"Service '{name}' depends on '{dependency}', "
                    f"which is not a service in this file.",
                    _line_of_key(text, "depends_on", after=line),
                    "depends_on refers to service keys, not container names.",
                )
            )

    return findings


def _secret_findings(
    text: str, name: str, service: dict, line: int
) -> list[Finding]:
    findings: list[Finding] = []
    for key, value in _environment_items(service.get("environment")):
        lowered = key.lower()
        if not any(hint in lowered for hint in _SECRET_HINTS):
            continue
        # An interpolated value keeps the secret out of the file, which is fine.
        if not value or "${" in value:
            continue
        findings.append(
            Finding(
                WARNING,
                f"Service '{name}' has a literal value for '{key}'.",
                _line_of_key(text, key, after=line),
                "Anyone reading the file (or the repository) can see it. Move it "
                "to a .env file and interpolate, or use Docker secrets.",
            )
        )
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# Extraction helpers — compose accepts several shapes for most of these fields
# ──────────────────────────────────────────────────────────────────────────────


def _top_level_names(data: dict, key: str) -> list[str]:
    section = data.get(key)
    if isinstance(section, dict):
        return [str(name) for name in section]
    if isinstance(section, list):
        return [str(name) for name in section if isinstance(name, (str, int))]
    return []


def _image_tag(image: str) -> str:
    """Return the tag part of an image reference, or ``""`` if there is none.

    Careful with registries that carry a port (``registry:5000/app``): the colon
    that matters is the one after the final slash.
    """
    if "@" in image:
        return "digest"  # Pinned by digest — the strongest form.
    last_segment = image.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return last_segment.rsplit(":", 1)[-1]
    return ""


def _published_ports(ports: Any) -> list[str]:
    """Host ports a service claims, as ``"port/protocol"`` strings.

    Short syntax without a host side (``"3000"``) gets an ephemeral port, so it
    can never collide and is skipped.
    """
    if not isinstance(ports, list):
        return []

    claimed: list[str] = []
    for entry in ports:
        if isinstance(entry, dict):
            host = entry.get("published")
            protocol = str(entry.get("protocol", "tcp"))
            if host not in (None, ""):
                claimed.extend(_expand_range(str(host), protocol))
            continue
        if not isinstance(entry, (str, int)):
            continue

        spec = str(entry)
        protocol = "tcp"
        if "/" in spec:
            spec, _, protocol = spec.rpartition("/")

        parts = spec.split(":")
        if len(parts) < 2:
            continue  # "3000" — container port only.
        host = parts[-2]  # Handles "ip:host:container" and "host:container".
        claimed.extend(_expand_range(host, protocol or "tcp"))
    return claimed


def _expand_range(host: str, protocol: str) -> list[str]:
    """Expand ``"8000-8002"`` into individual ports; ignore silly ranges."""
    if "-" not in host:
        return [f"{host}/{protocol}"] if host.isdigit() else []
    start, _, end = host.partition("-")
    if not (start.isdigit() and end.isdigit()):
        return []
    first, last = int(start), int(end)
    if last < first or last - first > 1024:
        return []
    return [f"{port}/{protocol}" for port in range(first, last + 1)]


def _named_volume_refs(volumes: Any) -> list[str]:
    """Named volumes a service mounts, excluding bind mounts and anonymous ones."""
    if not isinstance(volumes, list):
        return []

    names: list[str] = []
    for entry in volumes:
        if isinstance(entry, dict):
            if entry.get("type") == "volume" and entry.get("source"):
                names.append(str(entry["source"]))
            continue
        if not isinstance(entry, str):
            continue
        source = entry.split(":", 1)[0]
        # A path (absolute, relative, or ~) is a bind mount, not a named volume.
        if source and not source.startswith((".", "/", "~", "$")):
            names.append(source)
    return names


def _network_refs(networks: Any) -> list[str]:
    if isinstance(networks, dict):
        return [str(name) for name in networks]
    if isinstance(networks, list):
        return [str(name) for name in networks if isinstance(name, str)]
    return []


def _depends_on_refs(depends_on: Any) -> list[str]:
    if isinstance(depends_on, dict):
        return [str(name) for name in depends_on]
    if isinstance(depends_on, list):
        return [str(name) for name in depends_on if isinstance(name, str)]
    if isinstance(depends_on, str):
        return [depends_on]
    return []


def _environment_items(environment: Any) -> list[tuple[str, str]]:
    """Normalise both environment shapes into ``(key, value)`` pairs."""
    if isinstance(environment, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in environment.items()]
    if isinstance(environment, list):
        items = []
        for entry in environment:
            if isinstance(entry, str) and "=" in entry:
                key, _, value = entry.partition("=")
                items.append((key.strip(), value.strip()))
        return items
    return []


def _line_of_key(text: str, key: str, after: int = 0) -> int:
    """1-based line of ``key:``, searching from line ``after`` onwards.

    Used to anchor findings without a line-aware YAML loader. Searching forward
    from the service's own line is what keeps a common key like ``image`` from
    resolving to the first occurrence in the file.
    """
    if not key:
        return 0
    pattern = re.compile(rf"^\s*(?:-\s*)?{re.escape(key)}\s*:", re.MULTILINE)
    lines = text.splitlines()
    start_offset = 0
    if after > 1:
        start_offset = sum(len(line) + 1 for line in lines[: after - 1])

    match = pattern.search(text, start_offset)
    if match is None and start_offset:
        match = pattern.search(text)  # Fall back to anywhere in the file.
    if match is None:
        return after
    return text.count("\n", 0, match.start()) + 1


# ──────────────────────────────────────────────────────────────────────────────
# Inspection and diffing
# ──────────────────────────────────────────────────────────────────────────────


def service_names(text: str) -> list[str]:
    """Service keys in a compose document, in file order. Empty on bad input."""
    result = parse(text)
    if result.data is None:
        return _service_names_by_regex(text)
    services = result.data.get("services")
    if not isinstance(services, dict):
        return []
    return [str(name) for name in services]


def _service_names_by_regex(text: str) -> list[str]:
    """Best-effort service names when the document will not parse.

    The editor still wants to offer a service filter while the file is mid-edit
    and temporarily invalid — and this is the only path there is when PyYAML is
    not installed.

    Only keys at the first indentation depth seen inside ``services:`` count.
    Without that, a block opener one level down (``ports:``, ``healthcheck:``)
    looks exactly like a service key and gets reported as one.
    """
    names: list[str] = []
    in_services = False
    depth: Optional[int] = None
    for line in text.splitlines():
        if re.match(r"^services\s*:", line):
            in_services = True
            continue
        if not in_services:
            continue
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break  # A new top-level key ends the block.
        match = re.match(r"^(\s{1,4})([A-Za-z0-9._-]+)\s*:\s*(?:#.*)?$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        if depth is None:
            depth = indent
        if indent == depth:
            names.append(match.group(2))
    return names


def summarize(findings: list[Finding]) -> str:
    """One-line summary of a lint run, for the editor's status bar."""
    if not findings:
        return "No issues found"
    counts = {ERROR: 0, WARNING: 0, INFO: 0}
    for finding in findings:
        counts[finding.level] = counts.get(finding.level, 0) + 1
    parts = [
        f"{count} {level}{'s' if count != 1 else ''}"
        for level, count in counts.items()
        if count
    ]
    return ", ".join(parts)


def diff(original: str, edited: str, path: str = "compose file") -> str:
    """Unified diff between two versions of a file. Empty string if identical."""
    if original == edited:
        return ""
    lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        edited.splitlines(keepends=True),
        fromfile=f"{path} (on host)",
        tofile=f"{path} (edited)",
        n=3,
    )
    return "".join(lines)
