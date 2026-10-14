"""Copy examples/invoices into the video as TypeScript, so the video shows the real files."""

import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[3]
ex = root / "examples" / "invoices"
legacy = (ex / "legacy_etl.py").read_text().rstrip("\n").split("\n")
pipeline = (ex / "pipeline.yaml").read_text().rstrip("\n").split("\n")
out = pathlib.Path(__file__).resolve().parents[1] / "src" / "compress" / "sources.ts"
out.write_text(
    "// Generated from examples/invoices by `npm run sources`. Do not edit by hand.\n"
    f"export const LEGACY: string[] = {json.dumps(legacy, ensure_ascii=False)};\n"
    f"export const PIPELINE: string[] = {json.dumps(pipeline, ensure_ascii=False)};\n"
)
print(f"legacy {len(legacy)} lines, pipeline {len(pipeline)} lines -> {out}")
