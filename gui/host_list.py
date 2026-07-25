"""Sidebar list of configured hosts, as selectable cards."""

from typing import Callable, Optional

import customtkinter as ctk

from core import distro
from models.host import UPDATES_FAILED, UPDATES_UNKNOWN, Host

_ACTIVE_COLOR = "#1E6B9E"
_INACTIVE_COLOR = "transparent"

HostAction = Callable[[Host], None]


class HostList(ctk.CTkScrollableFrame):
    """Scrollable host cards.

    Cards are built once per host in :meth:`set_hosts` and thereafter updated in
    place by :meth:`refresh`. Rebuilding the widget tree on every value change —
    which a parallel update check triggers once per host — made the sidebar
    flicker and cost O(hosts²) widget churn for no benefit.
    """

    def __init__(
        self,
        parent,
        *,
        on_select: HostAction,
        on_edit: HostAction,
        on_remove: HostAction,
        **kwargs,
    ):
        super().__init__(parent, label_text="", **kwargs)
        self.grid_columnconfigure(0, weight=1)

        self._on_select = on_select
        self._on_edit = on_edit
        self._on_remove = on_remove

        self._cards: list[_HostCard] = []
        self._active: Optional[Host] = None

    def set_hosts(self, hosts: list[Host]) -> None:
        """Rebuild the list. Call when a host is added, removed, or edited."""
        for card in self._cards:
            card.destroy()
        self._cards = [
            _HostCard(
                self,
                host,
                on_select=self._on_select,
                on_edit=self._on_edit,
                on_remove=self._on_remove,
            )
            for host in hosts
        ]
        for card in self._cards:
            card.pack(fill="x", pady=3)
        self.refresh()

    def set_active(self, host: Optional[Host]) -> None:
        """Highlight ``host`` as the selected one."""
        self._active = host
        self.refresh()

    def refresh(self) -> None:
        """Re-read every host and update its card in place."""
        for card in self._cards:
            card.refresh(active=card.host is self._active)


class _HostCard(ctk.CTkFrame):
    """One host: name, address, detected OS, and pending-update state."""

    def __init__(
        self,
        parent,
        host: Host,
        *,
        on_select: HostAction,
        on_edit: HostAction,
        on_remove: HostAction,
    ):
        super().__init__(parent, corner_radius=8)
        self.host = host

        self.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        inner.grid_columnconfigure(0, weight=1)

        self._name = ctk.CTkLabel(
            inner, font=ctk.CTkFont(weight="bold"), anchor="w", justify="left"
        )
        self._name.grid(row=0, column=0, sticky="w")

        self._address = self._detail_label(inner, "gray70")
        self._address.grid(row=1, column=0, sticky="w")

        self._os = self._detail_label(inner, "gray60")
        self._updates = self._detail_label(inner, "gray60")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=0, column=1, padx=4, pady=4)
        ctk.CTkButton(
            buttons, text="✎", width=28, height=28, command=lambda: on_edit(host)
        ).pack(pady=1)
        ctk.CTkButton(
            buttons,
            text="✕",
            width=28,
            height=28,
            fg_color="#883333",
            hover_color="#AA4444",
            command=lambda: on_remove(host),
        ).pack(pady=1)

        # Clicking anywhere that is not a button selects the host.
        for widget in (self, inner, self._name, self._address, self._os, self._updates):
            widget.bind("<Button-1>", lambda _event: on_select(host))

    @staticmethod
    def _detail_label(parent, color: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=color,
            anchor="w",
            justify="left",
        )

    def refresh(self, *, active: bool) -> None:
        host = self.host
        self.configure(fg_color=_ACTIVE_COLOR if active else _INACTIVE_COLOR)
        self._name.configure(text=host.display_name)
        self._address.configure(text=host.address)

        _set_row(self._os, row=2, text=host.os_pretty)

        text, color = _update_summary(host)
        _set_row(self._updates, row=3, text=text, color=color)


def _update_summary(host: Host) -> tuple[str, str]:
    """Return the ``(text, colour)`` describing this host's update state."""
    count = host.pending_updates
    if count == UPDATES_UNKNOWN:
        return "", "gray60"
    if count == UPDATES_FAILED:
        return "⚠ update check failed", "#FF8A80"
    if count == 0:
        return "✓ up to date", "#4CAF50"
    plural = "" if count == 1 else "s"
    return f"⬆ {count} update{plural} available", "#FFA040"


def _set_row(label: ctk.CTkLabel, *, row: int, text: str, color: str = "") -> None:
    """Show ``label`` with ``text``, or hide the row entirely when empty."""
    if not text:
        label.grid_remove()
        return
    label.configure(text=text, **({"text_color": color} if color else {}))
    label.grid(row=row, column=0, sticky="w")


def os_badge_color(host: Optional[Host]) -> str:
    """Badge colour for the header bar's OS chip."""
    if host is None:
        return distro.DEFAULT_BRAND_COLOR
    return distro.brand_color(host.os_info, host.os_like)
