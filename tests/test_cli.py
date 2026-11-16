import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("odooutil", "record-set-batch"),
        ("odooutil.schema", "field-create"),
        ("odooutil.query", "search-read"),
        ("odooutil.report", "--spec"),
    ],
)
def test_module_entrypoints_work_without_credentials(module: str, command: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert command in result.stdout
