"""Offline tests for the devin-judge meta-package shim.

No model downloads, no network: entry-point wiring is checked against the
installed distribution metadata when available, otherwise against a wheel
built locally via hatchling's PEP 517 API.
"""

import importlib.metadata
import os
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_SCRIPTS = {
    "devin-judge": "devin_judge_shim:_main",
    "poordjaevin": "devin_judge_shim:_main",
}


def _installed_console_scripts() -> dict | None:
    """Entry points from the installed devin-judge dist, or None if absent."""
    try:
        dist = importlib.metadata.distribution("devin-judge")
    except importlib.metadata.PackageNotFoundError:
        return None
    return {
        ep.name: ep.value
        for ep in dist.entry_points
        if ep.group == "console_scripts"
    }


def _wheel_texts(tmp_path: Path) -> tuple[str, str]:
    """Build the wheel in-process (no isolation, no network) and return
    (entry_points.txt, METADATA) contents."""
    hatchling_build = pytest.importorskip(
        "hatchling.build",
        reason="devin-judge not installed and hatchling unavailable to build",
    )
    cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    try:
        wheel_name = hatchling_build.build_wheel(str(tmp_path))
    finally:
        os.chdir(cwd)
    with zipfile.ZipFile(tmp_path / wheel_name) as wheel:
        names = wheel.namelist()
        ep_name = next(n for n in names if n.endswith("entry_points.txt"))
        meta_name = next(n for n in names if n.endswith("METADATA"))
        return (
            wheel.read(ep_name).decode(),
            wheel.read(meta_name).decode(),
        )


def test_both_console_scripts_registered(tmp_path):
    scripts = _installed_console_scripts()
    if scripts is not None:
        for name, target in EXPECTED_SCRIPTS.items():
            assert scripts.get(name) == target
        return
    entry_points_txt, _ = _wheel_texts(tmp_path)
    for name, target in EXPECTED_SCRIPTS.items():
        assert f"{name} = {target}" in entry_points_txt


def test_metadata_declares_poordjaevin_dependency(tmp_path):
    try:
        requires = importlib.metadata.metadata("devin-judge").get_all(
            "Requires-Dist"
        ) or []
    except importlib.metadata.PackageNotFoundError:
        _, metadata_txt = _wheel_texts(tmp_path)
        assert "Name: devin-judge" in metadata_txt
        requires = [
            line.split(":", 1)[1].strip()
            for line in metadata_txt.splitlines()
            if line.startswith("Requires-Dist:")
        ]
    assert any(r.startswith("poordjaevin") for r in requires)


def test_main_resolves_to_poordjaevin_cli_main():
    shim = pytest.importorskip("devin_judge_shim")
    cli = pytest.importorskip("poordjaevin.cli")
    assert callable(shim._main)
    assert shim._main is cli.main


def test_help_dispatch_exits_zero():
    shim = pytest.importorskip("devin_judge_shim")
    pytest.importorskip("poordjaevin.cli")
    with pytest.raises(SystemExit) as excinfo:
        shim._main(["--help"])
    assert excinfo.value.code == 0
