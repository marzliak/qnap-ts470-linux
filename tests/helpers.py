"""Shared test helpers.

The executables are installed without a .py suffix, so they are loaded by path
rather than imported by name. Neither script performs I/O at import time, which
is what makes this possible and is worth keeping true.
"""

import importlib.util
import os
import sys
import tempfile

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


_TEST_LOCK_ROOT = None


def load_fan():
    """The fan program, with its writer-lock root pointed somewhere disposable.

    The production default is /run/qnap-tsx70, a real path on this host, and
    `run` and `calibrate` create it before their first register write. No test
    may do that, so the reroot happens once here rather than in every test
    class that happens to start a control loop - a class that forgot would
    otherwise leave a lock file in /run and nothing would say so.

    Subprocesses get the same directory through QNAP_TSX70_LOCK_DIR, which is
    how two of them can be made to contend on purpose.
    """
    global _TEST_LOCK_ROOT
    module = load_script("bin/qnap-tsx70-fancontrol", "qnap_tsx70_fancontrol")
    if _TEST_LOCK_ROOT is None:
        _TEST_LOCK_ROOT = tempfile.mkdtemp(prefix="qnap-tsx70-test-locks-")
    module.LOCK_ROOT = _TEST_LOCK_ROOT
    return module


def write_sysfs_tree(root, spec):
    """Create a fake sysfs tree from {relative_path: contents}."""
    for relative, contents in spec.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(contents)
    return root


import importlib.machinery  # noqa: E402  (used by load_script above)
