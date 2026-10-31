"""One real Gemma 4 image call using the worker's exact prompt and validator.

Usage:
    export GEMINI_API_KEY=...
    python scripts/smoke_gemma.py path/to/photo.jpg [--model gemma-4-26b-a4b-it]

Sends only the photo plus demo context (no email). Prints the raw model text,
then whether it passes the report schema.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.report_schema import ReportValidationError, extract_json, validate_model_output  # noqa: E402
from app.worker import DEFAULT_MODEL, GemmaClient, build_prompt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--model", default=os.environ.get("GEMMA_MODEL", DEFAULT_MODEL))
    ap.add_argument("--observations", default="trees_vegetation,missing_seating")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Set GEMINI_API_KEY first.")
        return 2
    path = Path(args.image)
    data = path.read_bytes()
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"

    sub = {
        "reference_id": "BUS-2026-SM0KETST",
        "created_at": datetime.now(timezone.utc),
        "location": {"type": "address", "address": "Demo bus stop location"},
        "observations": args.observations.split(","),
    }
    client = GemmaClient(key, args.model)
    started = time.perf_counter()
    raw = client.generate(data, mime, build_prompt(sub))
    print(f"model={args.model} latency={time.perf_counter() - started:.1f}s\n")
    print("---- raw model output ----")
    print(raw)
    print("--------------------------")
    try:
        part = validate_model_output(extract_json(raw))
    except ReportValidationError as exc:
        print(f"SCHEMA: FAIL ({exc})")
        return 1
    print("SCHEMA: PASS")
    print(json.dumps(part, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
