from pathlib import Path


def test_agent_contracts_are_identical() -> None:
    root = Path(__file__).parents[1]
    assert (root / "AGENTS.md").read_bytes() == (root / "CLAUDE.md").read_bytes()
