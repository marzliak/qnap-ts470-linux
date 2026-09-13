#!/usr/bin/env python3
"""The canonical fan writer lock, for the shell scripts that must hold it.

`qnap-tsx70-fancontrol run` and `calibrate` serialise against each other on a
global lock under /run before their first register write. The lifecycle
scripts - install, rollback, uninstall - replace and delete the very binary
those writers run, and a point-in-time "no fan process is running" check
cannot cover the window between that check and the `rm`: a `systemctl start`
or an operator's `calibrate` landing in the gap produces exactly the situation
the check exists to prevent, a live writer whose executable is being removed
underneath it.

So the lifecycle scripts take the same lock, and hold it across the recheck,
the verified handback and every replacement or removal. This program is how a
shell script does that on the documented Debian baseline without depending on
util-linux's `flock(1)`:

    path="$(python3 scripts/qnap_lock.py path)"
    exec {fd}>>"$path"                              # the shell owns the FD
    python3 scripts/qnap_lock.py acquire --fd "$fd" # lock it and exit
    ...                                             # the lock is still held
    exec {fd}>&-                                    # released

The lock lives on the open file description, not on this process, so it
survives this program exiting and belongs to the shell for exactly as long as
the shell keeps the descriptor open. That is what makes cleanup unconditional:
a `trap` that closes the FD releases it, and so does the kernel on any exit
this script never gets to handle, including SIGKILL. There is no lock file to
leave behind claiming an owner that is gone.

The path is not this program's to invent. It is read from
bin/qnap-tsx70-fancontrol - the same `writer_lock_paths()` the writers
themselves call - so there is one definition of "the canonical lock" in the
repository and no way for the two sides to drift apart. Only the global lock
is taken here: every writer takes that one first, whatever controller it
resolved and whatever `--cache` it was given, so holding it is enough to
exclude all of them, and taking it needs no device discovery in the shell.
"""

import argparse
import fcntl
import importlib.machinery
import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAN_PROGRAM = os.path.join(REPO_ROOT, "bin", "qnap-tsx70-fancontrol")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BUSY = 3


def load_fan_program(path=FAN_PROGRAM):
    """Import the fan program by path. It does no I/O at import time.

    Deliberately the repository copy and nothing else. An installed binary may
    be older than this candidate, and letting a caller name the program would
    make the lock path selectable by whoever runs the script - which is the
    one property this must not have.
    """
    loader = importlib.machinery.SourceFileLoader("qnap_fancontrol_lock", path)
    spec = importlib.util.spec_from_loader("qnap_fancontrol_lock", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def canonical_lock_path(path=FAN_PROGRAM):
    """The global writer lock every fan writer takes first.

    `writer_lock_paths()` returns the global lock followed by the
    per-controller one; the global lock is index 0 by definition and the
    writers take them in that order.
    """
    return load_fan_program(path).writer_lock_paths()[0]


def cmd_path(_args):
    print(canonical_lock_path())
    return EXIT_OK


def cmd_acquire(args):
    """Take an exclusive non-blocking lock on a descriptor the caller owns.

    The descriptor is inherited, not opened here, so the lock outlives this
    process: closing it is the caller's release, and the caller's death is the
    kernel's.
    """
    try:
        os.fstat(args.fd)
    except OSError as exc:
        print("file descriptor %d is not open: %s" % (args.fd, exc),
              file=sys.stderr)
        return EXIT_ERROR
    try:
        fcntl.flock(args.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another process holds the fan writer lock", file=sys.stderr)
        return EXIT_BUSY
    try:
        os.write(args.fd, b"%d\n" % os.getpid())
    except OSError:
        # The owner is the open file description, not this line. A lock that
        # cannot be annotated is still a lock.
        pass
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qnap_lock.py",
        description="The canonical qnap-tsx70 fan writer lock.")
    sub = parser.add_subparsers(dest="command", required=True)

    where = sub.add_parser("path", help="print the canonical global lock path")
    where.set_defaults(func=cmd_path)

    take = sub.add_parser(
        "acquire", help="lock an already-open descriptor, then exit")
    take.add_argument("--fd", type=int, required=True,
                      help="descriptor the caller opened on the lock file")
    take.set_defaults(func=cmd_acquire)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print("qnap_lock.py: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
