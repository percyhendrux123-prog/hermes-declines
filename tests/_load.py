"""Load the plugin (repo root) as the package `hermes_declines` without installing anything."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(os.path.realpath(__file__)).parents[1]
import tempfile
SCRATCH = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def load():
    if "hermes_declines" in sys.modules:
        return sys.modules["hermes_declines"], sys.modules["hermes_declines.core"]
    spec = importlib.util.spec_from_file_location(
        "hermes_declines", str(ROOT / "__init__.py"), submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_declines"] = mod
    spec.loader.exec_module(mod)
    return mod, sys.modules["hermes_declines.core"]
