from agentl import tool

CALLS: list[str] = []


@tool(read_only=True)
def lookup(code: str) -> dict:
    """Look up a product code."""
    CALLS.append(code)
    return {"code": code, "name": f"product {code}"}


@tool(read_only=False)
def save_note(text: str) -> str:
    """Save a note somewhere."""
    return "saved"


def plain(x: str) -> str:
    return x
