"""Allow `python -m gui` from the project root."""
import sys

from gui.main import main

if __name__ == "__main__":
    sys.exit(main())
