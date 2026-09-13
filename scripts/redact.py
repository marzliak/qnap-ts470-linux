#!/usr/bin/env python3
"""Redaction filter for the qnap-tsx70 diagnostic bundle.

Reads the bundle on stdin and writes it to stdout with identifying values
replaced. It exists as a Python filter rather than a `sed` script because a
single regular expression cannot recognise a compressed IPv6 address without
also eating ordinary colon-separated text: `fe80::1` has to go, and
`22:54:01` has to stay. Candidates are found by a deliberately loose pattern
and then confirmed by `ipaddress`, which is the only parser here that decides
what an address is.

Placeholders: [hostname] [mac] [ip] [ipv6] [uuid] [serial removed] [redacted]

Redaction is a safety net, not a promise. The bundle is still meant to be read
before it is posted anywhere.
"""

import argparse
import ipaddress
import re
import sys

MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")

UUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
                     r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")

# Case is meaningful: `SERIAL` in capitals is a smartctl field, while the
# lowercase word appears in this bundle's own "Serial ports" heading, which
# must survive.
SERIAL_LINE_RE = re.compile(r"[Ss]erial [Nn]umber|SERIAL|[Ss]erial#"
                            r"|LU WWN|WWN|IEEE EUI-64|[Ww]orld [Ww]ide [Nn]ame")

SERIAL_JSON_RE = re.compile(r'"serial_number"\s*:\s*"[^"]*"')

TAGGED_ID_RE = re.compile(r"(PARTUUID|UUID|ID_SERIAL|WWN)=[^\s\"]*",
                          re.IGNORECASE)

# Loose candidates. Both are confirmed by ipaddress before anything is cut.
# The lookbehinds exclude only alphanumerics. Excluding `:` and `.` as well
# looked tidier and quietly stopped `addr:192.0.2.1` and `addr:fe80::1` from
# being recognised at all.
IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:\.[0-9]{1,3}){0,3}"
    r"(?:%[0-9A-Za-z_.\-]+)?"
    r"(?:/[0-9]{1,3})?")

IPV4_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
    r"(?:/[0-9]{1,2})?"
    r"(?![0-9A-Za-z.])")


def _split_suffixes(token):
    """Return (address, zone, prefix) for one candidate token."""
    body, prefix = token, None
    if "/" in body:
        body, _, prefix = body.partition("/")
    zone = None
    if "%" in body:
        body, _, zone = body.partition("%")
    return body, zone, prefix


def is_ipv6(token):
    body, zone, prefix = _split_suffixes(token)
    if prefix is not None and not (prefix.isdigit() and 0 <= int(prefix) <= 128):
        return False
    if zone is not None and not zone:
        return False
    try:
        ipaddress.IPv6Address(body)
    except ValueError:
        return False
    return True


def is_ipv4(token):
    body, zone, prefix = _split_suffixes(token)
    if zone is not None:
        return False
    if prefix is not None and not (prefix.isdigit() and 0 <= int(prefix) <= 32):
        return False
    try:
        ipaddress.IPv4Address(body)
    except ValueError:
        return False
    return True


def _replace_longest_valid(match, predicate, placeholder):
    """Redact the longest leading part of the match that is a real address.

    The loose pattern happily swallows a trailing colon in `fe80::1: down`.
    Trimming from the right until `ipaddress` accepts what is left keeps the
    address redacted without also eating the punctuation after it.
    """
    token = match.group(0)
    for end in range(len(token), 1, -1):
        head = token[:end]
        if predicate(head):
            return placeholder + token[end:]
    return token


def redact_line(line, hostnames=()):
    """Redact one line. The trailing newline, if any, must be stripped first."""
    for name in hostnames:
        if not name:
            continue
        # Word-anchored: a short hostname must not be substituted inside an
        # unrelated identifier such as a board model number.
        line = re.sub(r"\b%s\b" % re.escape(name), "[hostname]", line,
                      flags=re.IGNORECASE)

    if SERIAL_LINE_RE.search(line):
        return "[serial removed]"

    line = MAC_RE.sub("[mac]", line)
    # IPv6 first: an IPv4-mapped address is reported as one address, not as a
    # v6 husk wrapped around a redacted v4.
    line = IPV6_CANDIDATE_RE.sub(
        lambda m: _replace_longest_valid(m, is_ipv6, "[ipv6]"), line)
    line = IPV4_CANDIDATE_RE.sub(
        lambda m: _replace_longest_valid(m, is_ipv4, "[ip]"), line)
    line = UUID_RE.sub("[uuid]", line)
    line = SERIAL_JSON_RE.sub('"serial_number": "[serial removed]"', line)
    line = TAGGED_ID_RE.sub(lambda m: "%s=[redacted]" % m.group(1), line)
    return line


def redact_stream(source, sink, hostnames=()):
    for raw in source:
        text = raw.decode("utf-8", errors="replace")
        newline = ""
        if text.endswith("\n"):
            text, newline = text[:-1], "\n"
        sink.write((redact_line(text, hostnames) + newline).encode("utf-8"))
    sink.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="redact.py",
        description="Redact identifying values from a diagnostic bundle.")
    parser.add_argument("--hostname", action="append", default=[],
                        metavar="NAME",
                        help="also replace this name (may be repeated)")
    args = parser.parse_args(argv)
    try:
        redact_stream(sys.stdin.buffer, sys.stdout.buffer, args.hostname)
    except BrokenPipeError:
        return 1
    except OSError as exc:
        print("redact.py: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
