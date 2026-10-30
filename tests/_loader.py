"""Shared: load prboard.py from the repo root regardless of CWD."""
import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "prboard", os.path.join(_ROOT, "prboard.py"))
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)
