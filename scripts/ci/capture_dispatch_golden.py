#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate tests/models/dispatch_golden.json from the CURRENT router.

The golden table pins how models/model_router.py dispatches when tier
governance is switched off. Phase 5 turned the fifteen hand-written `_try_*`
methods into wrappers over one family dispatcher, and this file is what proves
that collapse changed nothing for deployments running with the flag off.

    python scripts/ci/capture_dispatch_golden.py

Running it REPLACES the expected values with whatever the code does today,
which defeats the point unless the behaviour change was intended. Regenerate
only alongside a reviewed change, and read the resulting diff.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# core.config loads .env. It has to run before anything imports
# auth/jwt_handler, which raises at import time on a missing JWT_SECRET —
# under pytest the plugin chain happens to get there first, but a bare script
# does not.
import core.config  # noqa: E402,F401

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402

sys.path.insert(0, str(ROOT / "tests" / "models"))
import test_dispatch_equivalence as t  # noqa: E402


def main() -> int:
    out = {"sync": {}, "stream": {}}
    for tier in t.TIERS:
        for scenario, (modes, open_brk, kw) in t.SCENARIOS.items():
            key = f"{tier}|{scenario}"
            for kind, record in (("sync", t.sync_record), ("stream", t.stream_record)):
                mp = MonkeyPatch()
                try:
                    build = t.harness.__wrapped__(mp)
                    r, log = build(modes, open_brk)
                    out[kind][key] = record(r, log, tier, **kw)
                finally:
                    mp.undo()

    dest = ROOT / "tests" / "models" / "dispatch_golden.json"
    dest.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {dest} — {len(out['sync'])} sync + {len(out['stream'])} stream records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
