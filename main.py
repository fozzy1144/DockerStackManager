import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

from gui.app import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
