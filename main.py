"""Entry point for Docker Stack Manager.

Run with ``python main.py``, or on Windows ``run.bat`` / a double-clicked
``run.pyw``, both of which start it without a console window.
"""

import os
import sys
import traceback

# Allow running as a script from any working directory — run.bat and a
# double-clicked launcher both land here without the project root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TITLE = "Docker Stack Manager"


def has_console() -> bool:
    """Whether anything written to stderr can be seen.

    Under ``pythonw`` and in the packaged executable there is no console and
    ``sys.stderr`` is ``None``.
    """
    return sys.stderr is not None


def report_failure(message: str) -> None:
    """Tell the user the app could not start, wherever they can see it.

    With a console that is stderr. Without one, a failed start would otherwise
    be a window that never appears and no explanation, so it is a dialog.
    """
    if has_console():
        print(message, file=sys.stderr)
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, _TITLE, 0x10)  # MB_ICONERROR
    except Exception:
        pass  # Not Windows, or no desktop: there is nowhere left to say it.


def main() -> int:
    try:
        from gui.app import App
    except ImportError as exc:
        report_failure(
            f"Missing dependency: {exc}\n\n"
            "Install the requirements first:\n"
            "    pip install -r requirements.txt"
        )
        return 1

    try:
        App().mainloop()
    except Exception:
        if has_console():
            raise
        report_failure(f"{_TITLE} stopped unexpectedly.\n\n{traceback.format_exc()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
