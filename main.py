"""Entry point for Docker Stack Manager.

Run with ``python main.py`` (or ``run.bat`` on Windows).
"""

import os
import sys

# Allow running as a script from any working directory — run.bat and a
# double-clicked main.py both land here without the project root on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        from gui.app import App
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        print("Install the requirements first:", file=sys.stderr)
        print("    pip install -r requirements.txt", file=sys.stderr)
        return 1

    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
