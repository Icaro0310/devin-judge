"""Skill/plugin surface contract: the shipped skill exists, has valid
frontmatter, stays advisory, and the plugin manifest is self-consistent."""

import json
from pathlib import Path

ADAPTERS = Path(__file__).parents[1] / "adapters"
SKILL = ADAPTERS / "skills" / "devin-judge" / "SKILL.md"
MANIFEST = ADAPTERS / ".devin-plugin" / "plugin.json"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md missing frontmatter"
    block = text.split("---", 2)[1]
    out = {}
    for line in block.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def test_skill_exists_with_required_frontmatter():
    assert SKILL.is_file()
    fm = _frontmatter(SKILL)
    assert fm["name"] == "devin-judge"
    assert fm["description"]


def test_skill_is_advisory_by_text():
    """The skill must not instruct the agent to execute or apply."""
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "advisory" in body
    for banned in ("--apply", "--force", "--delete", "--kill", "spawn"):
        assert banned not in body


def test_plugin_manifest_self_consistent():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "devin-judge"
    assert (ADAPTERS / "skills" / "devin-judge" / "SKILL.md").is_file()
    servers = manifest.get("mcpServers", {})
    assert "devin-judge" in servers
    # the declared server must be the package's own entrypoint.
    args = json.dumps(servers["devin-judge"])
    assert "poordjaevin" in args
    assert "serve" in args
