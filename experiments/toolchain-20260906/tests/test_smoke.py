# SPDX-License-Identifier: Apache-2.0
import runpy

import pytest

from visualworld_probe import __version__
from visualworld_probe.cli import main


def test_package_import_and_cli() -> None:
    assert __version__ == "0.0.0"
    assert main() == 0


def test_module_entry_point() -> None:
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("visualworld_probe", run_name="__main__")

    assert stopped.value.code == 0
