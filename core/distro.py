"""Linux distribution identity and the package-manager commands for each family.

All distro-specific knowledge lives here, in two lookup tables:

* :data:`_MANAGER_BY_ID` maps an ``/etc/os-release`` ``ID`` to a
  :class:`PackageManager`.
* :data:`_BRAND_COLORS` maps the same IDs to a badge colour for the GUI.

Nothing outside this module branches on which distro is on the other end of the
SSH connection: :mod:`core.ssh_client` just runs the strings it is handed, and
the GUI asks :func:`resolve` and :func:`brand_color` for what to use. Supporting
a new distro is a one-line change here.
"""

from dataclasses import dataclass

DEFAULT_BRAND_COLOR = "#555577"


@dataclass(frozen=True, slots=True)
class OSInfo:
    """Identity of a remote OS, as reported by ``/etc/os-release``."""

    id: str = ""
    """The ``ID`` field, e.g. ``"ubuntu"``. Empty when detection failed."""

    pretty: str = ""
    """The ``PRETTY_NAME`` field, e.g. ``"Ubuntu 24.04.1 LTS"``. For display."""

    like: str = ""
    """The ``ID_LIKE`` field, e.g. ``"debian"``. Space-separated parent IDs."""


@dataclass(frozen=True, slots=True)
class PackageManager:
    """How to count and apply pending package updates for one distro family."""

    name: str
    """Short name of the tool, e.g. ``"apt"``. Shown in the log."""

    check_cmd: str
    """Prints the number of pending updates on stdout. Runs as the login user."""

    update_cmd: str
    """Applies every pending update non-interactively. Requires root."""


# `apt-get --simulate upgrade` needs no privileges and no lock, and reports
# exactly what the update command below would install (one "Inst " line each),
# so the count the user sees always matches what the update will do.
APT = PackageManager(
    name="apt",
    check_cmd=(
        "apt-get --simulate --quiet -o Debug::NoLocking=1 upgrade 2>/dev/null "
        "| grep -c '^Inst '"
    ),
    update_cmd=(
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -y && "
        "apt-get -y -o Dpkg::Options::=--force-confold upgrade"
    ),
)

# `dnf check-update` lists one "name.arch  version  repo" row per pending
# package. Counting rows that begin with a package name is a heuristic — an
# "Obsoleting Packages" section can inflate it slightly — but it never needs
# root and never blocks on the metadata lock.
DNF = PackageManager(
    name="dnf",
    check_cmd=(
        "dnf --quiet check-update 2>/dev/null "
        "| grep -cE '^[[:alnum:]][^[:space:]]*[[:space:]]+[^[:space:]]+[[:space:]]'"
    ),
    update_cmd="dnf -y upgrade || yum -y upgrade",
)

PACMAN = PackageManager(
    name="pacman",
    check_cmd="pacman -Qu 2>/dev/null | wc -l",
    update_cmd="pacman -Syu --noconfirm",
)

APK = PackageManager(
    name="apk",
    check_cmd="apk list --upgradable 2>/dev/null | wc -l",
    update_cmd="apk update && apk upgrade",
)

ZYPPER = PackageManager(
    name="zypper",
    check_cmd="zypper --non-interactive list-updates 2>/dev/null | grep -c '^v '",
    update_cmd=(
        "zypper --non-interactive refresh && zypper --non-interactive update"
    ),
)

#: Fallback for unrecognised distros. Debian derivatives are by far the most
#: common thing to find behind an unfamiliar ``ID``.
DEFAULT_MANAGER = APT

_MANAGER_BY_ID: dict[str, PackageManager] = {
    # Debian family
    "debian": APT,
    "devuan": APT,
    "elementary": APT,
    "kali": APT,
    "linuxmint": APT,
    "pop": APT,
    "raspbian": APT,
    "ubuntu": APT,
    # Red Hat family
    "almalinux": DNF,
    "amzn": DNF,
    "centos": DNF,
    "fedora": DNF,
    "ol": DNF,
    "rhel": DNF,
    "rocky": DNF,
    # Arch family
    "arch": PACMAN,
    "endeavouros": PACMAN,
    "garuda": PACMAN,
    "manjaro": PACMAN,
    # Others
    "alpine": APK,
    "opensuse": ZYPPER,
    "opensuse-leap": ZYPPER,
    "opensuse-tumbleweed": ZYPPER,
    "sles": ZYPPER,
    "suse": ZYPPER,
}

_BRAND_COLORS: dict[str, str] = {
    "almalinux": "#083F8A",
    "alpine": "#0D597F",
    "arch": "#1793D1",
    "centos": "#932279",
    "debian": "#A80030",
    "endeavouros": "#7F3FBF",
    "fedora": "#3C6EB4",
    "linuxmint": "#87CF3E",
    "manjaro": "#35BF5C",
    "opensuse": "#73BA25",
    "opensuse-leap": "#73BA25",
    "opensuse-tumbleweed": "#73BA25",
    "pop": "#48B9C7",
    "raspbian": "#C51A4A",
    "rhel": "#EE0000",
    "rocky": "#10B981",
    "ubuntu": "#E95420",
}


def _candidates(os_id: str, os_like: str = ""):
    """Yield ``os_id`` then each ``ID_LIKE`` parent, normalised for lookup."""
    for candidate in (os_id, *os_like.split()):
        normalised = candidate.strip().strip('"').lower()
        if normalised:
            yield normalised


def resolve(os_id: str, os_like: str = "") -> PackageManager:
    """Return the package manager for ``os_id``, falling back to ``ID_LIKE``.

    The ``ID_LIKE`` fallback is what makes derivative distros work without an
    entry of their own — an unknown Ubuntu remix reporting ``ID_LIKE=ubuntu
    debian`` still resolves to :data:`APT`.
    """
    for candidate in _candidates(os_id, os_like):
        manager = _MANAGER_BY_ID.get(candidate)
        if manager is not None:
            return manager
    return DEFAULT_MANAGER


def is_recognized(os_id: str, os_like: str = "") -> bool:
    """Whether the distro was matched outright rather than guessed at."""
    return any(c in _MANAGER_BY_ID for c in _candidates(os_id, os_like))


def brand_color(os_id: str, os_like: str = "") -> str:
    """Return the badge colour for a distro, or a neutral grey if unknown."""
    for candidate in _candidates(os_id, os_like):
        color = _BRAND_COLORS.get(candidate)
        if color is not None:
            return color
    return DEFAULT_BRAND_COLOR
