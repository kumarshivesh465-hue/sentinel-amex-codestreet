#!/usr/bin/env python3
"""Run the whole test suite.

    python run_tests.py           # quiet
    python run_tests.py -v        # verbose

Standard library only, so no installs are required to test this project.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def main() -> int:
    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1
    suite = unittest.defaultTestLoader.discover(
        start_dir=os.path.join(ROOT, "tests"), top_level_dir=ROOT
    )
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
