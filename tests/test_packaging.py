"""Release metadata must agree across files, or the publish workflow ships a
MCP Registry entry that does not match the package on PyPI."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _server():
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_server_json_points_at_this_package_version():
    project, server = _project(), _server()
    package = server["packages"][0]

    assert package["identifier"] == project["name"]
    assert server["version"] == package["version"] == project["version"], (
        f"server.json ({server['version']}/{package['version']}) and pyproject.toml "
        f"({project['version']}) disagree: the MCP Registry would list a release "
        "that does not match the package on PyPI"
    )


def test_package_reports_its_own_version():
    import solidworks_mcp

    assert solidworks_mcp.__version__ == _project()["version"], (
        f"solidworks_mcp.__version__ is {solidworks_mcp.__version__}, but this release is "
        f"{_project()['version']}: anyone checking the installed version is misled"
    )


def test_readme_carries_the_registry_ownership_marker():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"mcp-name: {_server()['name']}" in readme, (
        "the MCP Registry rejects the publish: the PyPI description (README.md) "
        "must contain 'mcp-name: <server name>'"
    )
