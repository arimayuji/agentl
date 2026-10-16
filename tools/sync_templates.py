"""Copy every recipe's pipeline.yaml into agentl/templates/<name>.yaml (the recipe folder without
its number), so `agentl new --template` ships the same tested pipelines in the package.
Run after changing a recipe: python tools/sync_templates.py (tests/test_templates.py checks it)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES = ROOT / "examples" / "recipes"
TEMPLATES = ROOT / "agentl" / "templates"


def expected() -> dict[str, str]:
    """{template name: YAML text} from the recipes."""
    return {
        d.name.split("-", 1)[1]: (d / "pipeline.yaml").read_text(encoding="utf-8")
        for d in sorted(RECIPES.iterdir())
        if (d / "pipeline.yaml").is_file()
    }


def main() -> None:
    TEMPLATES.mkdir(exist_ok=True)
    want = expected()
    for old in TEMPLATES.glob("*.yaml"):
        if old.stem not in want:
            old.unlink()
    for name, text in want.items():
        (TEMPLATES / f"{name}.yaml").write_text(text, encoding="utf-8")
    print(f"{len(want)} templates in {TEMPLATES.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
