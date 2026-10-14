#!/usr/bin/env python3
"""Website Metadata Hunter. Run without arguments for an overview of every command."""

import sys

if sys.version_info < (3, 9):
    sys.exit("Website Metadata Hunter needs Python 3.9 or newer.")

from metahunter.cli.main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
