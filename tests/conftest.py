"""Prevent pytest collection from writing __pycache__ into the source tree.

The release build treats source-tree caches as a blocker, so running the test
suite must never leave bytecode behind. conftest.py is imported before test
modules are collected, which makes this flag effective for the whole run.
"""

import sys

sys.dont_write_bytecode = True
