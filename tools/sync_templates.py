"""Copy every recipe's pipeline.yaml into agentl/templates/<name>.yaml (the recipe folder without
its number) and every pattern's into agentl/templates/patterns/<name>.yaml, so
`agentl new --template` ships the same tested pipelines in the package.
Run after changing a recipe or a pattern: python tools/sync_templates.py (tests/test_templates.py
checks it)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES = ROOT / "examples" / "recipes"
PATTERNS = ROOT / "examples" / "patterns"
TEMPLATES = ROOT / "agentl" / "templates"


def expected(group: str = "recipes") -> dict[str, str]:
    """{template name: YAML text} from the recipes, or from the patterns."""
    if group == "patterns":
        return {
            d.name: (d / "pipeline.yaml").read_text(encoding="utf-8")
            for d in sorted(PATTERNS.iterdir())
            if (d / "pipeline.yaml").is_file()
        }
    return {
        d.name.split("-", 1)[1]: (d / "pipeline.yaml").read_text(encoding="utf-8")
        for d in sorted(RECIPES.iterdir())
        if (d / "pipeline.yaml").is_file()
    }


def main() -> None:
    for group, target in (("recipes", TEMPLATES), ("patterns", TEMPLATES / "patterns")):
        target.mkdir(exist_ok=True)
        want = expected(group)
        for old in target.glob("*.yaml"):
            if old.stem not in want:
                old.unlink()
        for name, text in want.items():
            (target / f"{name}.yaml").write_text(text, encoding="utf-8")
        print(f"{len(want)} {group} templates in {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
