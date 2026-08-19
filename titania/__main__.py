"""`py -m titania <cmd>` -- see titania.cli for the commands."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
