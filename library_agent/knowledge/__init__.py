"""Knowledge documents the agent can read (SPEC Layer 5, PLAN 4.6).
Restricted documents are only readable by the librarian role."""
from pathlib import Path

DIR = Path(__file__).parent
RESTRICTED = {"fine_policy"}


def list_documents(role: str) -> list[str]:
    docs = sorted(p.stem for p in DIR.glob("*.md"))
    return [d for d in docs if d not in RESTRICTED or role == "librarian"]


def read_document(name: str, role: str) -> str:
    name = Path(name).stem  # no path tricks
    if name in RESTRICTED and role != "librarian":
        return "This document is restricted to the librarian."
    path = DIR / f"{name}.md"
    if not path.exists():
        return f"No document called '{name}'. Available: {', '.join(list_documents(role))}."
    return path.read_text(encoding="utf-8")
