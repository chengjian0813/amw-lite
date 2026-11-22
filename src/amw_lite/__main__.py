"""Module entrypoint for `python -m amw_lite`."""

import sys

from amw_lite.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
