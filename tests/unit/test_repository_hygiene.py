"""Repository-level invariants: agent instructions in sync, source docs intact, no secrets."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<!-- SHARED-RULES-START -->"


def _shared(name: str) -> str:
    text = (ROOT / name).read_text(encoding="utf-8")
    assert MARKER in text, f"{name} missing shared-rules marker"
    return text.split(MARKER, 1)[1]


def test_claude_and_agents_instructions_identical():
    assert _shared("CLAUDE.md") == _shared("AGENTS.md")


def test_project_sources_match_manifest():
    src = ROOT / "docs" / "project-sources"
    manifest = json.loads((src / "ELIDE_SOURCES_SHA256.json").read_text(encoding="utf-8"))
    assert len(manifest) == 18
    for name, digest in manifest.items():
        assert hashlib.sha256((src / name).read_bytes()).hexdigest() == digest, name


def test_env_example_has_placeholders_only():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("PAYPAL_CLIENT_ID=", "PAYPAL_CLIENT_SECRET=")):
            assert "replace-with" in line
        if line.startswith("LLM_API_KEY="):
            assert line == "LLM_API_KEY="
