# SPDX-License-Identifier: Apache-2.0
"""A help/version-only entry point, without media or model side effects."""

import argparse
from collections.abc import Sequence

from visualworld import __version__


def build_parser() -> argparse.ArgumentParser:
    """Create the command parser without reading process arguments."""
    parser = argparse.ArgumentParser(
        prog="visualworld",
        description="VisualWorld: a local-first, evidence-backed world database from video.",
        epilog="Experimental scaffold only; ingestion, storage, and queries are not implemented.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Display help by default; argparse handles explicit help, version, and errors."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
