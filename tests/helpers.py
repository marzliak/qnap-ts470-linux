"""Shared test helpers.

The executables are installed without a .py suffix, so they are loaded by path
rather than imported by name. Neither script performs I/O at import time, which
is what makes this possible and is worth keeping true.
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script(relative_path, module_name):
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.join(REPO_ROOT, relative_path)
    loader = importlib.machinery.SourceFileLoader(module_name, path)
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


def load_lcd():
    return load_script("bin/qnap-tsx70-lcd", "qnap_tsx70_lcd")


def load_fan():
    return load_script("bin/qnap-tsx70-fancontrol", "qnap_tsx70_fancontrol")


def write_sysfs_tree(root, spec):
    """Create a fake sysfs tree from {relative_path: contents}."""
    for relative, contents in spec.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(contents)
    return root


import importlib.machinery  # noqa: E402  (used by load_script above)
