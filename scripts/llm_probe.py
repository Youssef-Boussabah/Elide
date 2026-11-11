"""List the provider's models and probe strict JSON-schema output on candidate models.

    uv run python scripts/llm_probe.py [--models MODEL_ID ...]

Reads LLM_PROVIDER / LLM_API_KEY / LLM_BASE_URL from the untracked .env. Prints model ids
and probe results only (never the key). Each probe is one tiny call counted against the
spend cap. Writes evidence/phase3/llm_probe_<UTC date>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from api.ai.adapter import LLMOutputError, LLMUnavailableError  # noqa: E402
from api.ai.openai_compat import KNOWN_PRICES, NEBIUS_BASE_URL, OpenAICompatibleAdapter  # noqa: E402
from api.ai.spend import SpendGuard  # noqa: E402
from api.config import get_settings  # noqa: E402


class Probe(BaseModel):
    amount: str
    currency: str


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(KNOWN_PRICES))
    args = ap.parse_args()
    s = get_settings()
    if not (s.llm_provider and s.llm_api_key):
        print("LLM_PROVIDER / LLM_API_KEY not configured in .env")
        return 2
    base = s.llm_base_url or NEBIUS_BASE_URL
    headers = {"Authorization": f"Bearer {s.llm_api_key.get_secret_value()}"}
    r = httpx.get(base.rstrip("/") + "/models", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"model list failed: HTTP {r.status_code}")
        return 1
    ids = sorted(m.get("id", "") for m in r.json().get("data", []))
    print(f"{len(ids)} models available")
    results = []
    for model in args.models:
        entry = {"model": model, "listed": model in ids}
        if not entry["listed"] or model not in KNOWN_PRICES:
            entry["probe"] = "skipped (not listed or no price)"
            results.append(entry)
            continue
        a = OpenAICompatibleAdapter(
            provider=s.llm_provider,
            base_url=base,
            api_key=s.llm_api_key,
            model=model,
            prices=KNOWN_PRICES[model],
            spend=SpendGuard(s.llm_spend_cap_usd),
        )
        try:
            out = a.generate_structured(
                system="Extract the amount and currency exactly as written. Return JSON.",
                user="Keep $600 for payroll.",
                schema=Probe,
                schema_name="probe",
                prompt_version="probe-v1",
                max_tokens=200,
            )
            entry.update(probe="ok", value=out.value.model_dump(), call=out.call.as_dict())
        except (LLMOutputError, LLMUnavailableError) as exc:
            entry["probe"] = f"failed: {exc}"
        results.append(entry)
        print(model, entry["probe"])
    out_dir = ROOT / "evidence" / "phase3"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    path = out_dir / f"llm_probe_{now.date().isoformat()}.json"
    path.write_text(
        json.dumps(
            {
                "evidence_class": "provider observation (real model calls)",
                "at": now.isoformat(timespec="seconds"),
                "provider": s.llm_provider,
                "base_url": base,
                "models_listed": ids,
                "probes": results,
            },
            indent=1,
        )
        + "\n",
        "utf-8",
    )
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
