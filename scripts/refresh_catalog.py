#!/usr/bin/env python3
"""Re-bake the catalog snapshots shipped in the image.

Pulls the CLIProxyAPI catalog files and the models.dev catalog into
``cpa_context_bridge/data/``. Run by the scheduled GitHub Action so the baked
fallback tracks upstream; also runnable locally.

Exits non-zero only on a hard failure (network + no existing file). If a fetch
fails but a baked copy exists, the old copy is kept and we exit 0.
"""

from __future__ import annotations

import json
import sys
import urllib.request

from cpa_context_bridge.catalog import CODEX_URL, MODELS_URL, MODELSDEV_URL, DATA_DIR

TARGETS = {
    CODEX_URL: DATA_DIR / "codex_client_models.json",
    MODELS_URL: DATA_DIR / "models.json",
    MODELSDEV_URL: DATA_DIR / "modelsdev.json",
}

# models.dev is large (~2MB); keep it compact, pretty-print the small CPA files.
COMPACT = {DATA_DIR / "modelsdev.json"}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cpa-context-bridge-refresh"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed trusted URLs
        return resp.read()


def main() -> int:
    changed = False
    for url, path in TARGETS.items():
        try:
            raw = fetch(url)
            parsed = json.loads(raw)  # validate it is JSON before writing
            if path in COMPACT:
                text = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
            else:
                text = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if text != old:
                path.write_text(text, encoding="utf-8")
                changed = True
                print(f"updated {path.name} ({len(raw)} bytes)")
            else:
                print(f"unchanged {path.name}")
        except Exception as exc:  # noqa: BLE001
            if path.exists():
                print(f"WARN: fetch failed for {path.name}, keeping baked copy: {exc}")
            else:
                print(f"ERROR: fetch failed for {path.name} and no baked copy: {exc}")
                return 1
    print("changed" if changed else "no-change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
