#!/usr/bin/env python3
"""Source-tree compatibility wrapper; installed commands use ``corax.cli``."""

from corax.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
