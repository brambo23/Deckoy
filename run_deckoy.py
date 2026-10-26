"""PyInstaller entry point (the packaged exe can't use `python -m deckoy`)."""
import sys

from deckoy.app import main

if __name__ == "__main__":
    sys.exit(main())
