"""Start the dev server with the Alibaba backend open for THIS PROCESS ONLY.

`.env` stays at `LLM_BACKEND=local` / `ALIBABA_LLM_ALLOW_LIVE_REQUESTS=false`, because
that file is what every test, management command and future shell inherits. Opening
it there would make the kill switch open for things nobody is watching; opening it
here scopes the decision to the window the operator is actually looking at.

`load_dotenv` does not override an existing environment variable, so setting them
before Django is imported is what gives this process a different answer from the file.

EVERY CHAT TURN IN THIS SERVER IS A PAID REQUEST. There is no per-turn confirmation
in the UI — the confirmation is starting this script.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = sys.argv[1] if len(sys.argv) > 1 else "8002"

env = dict(os.environ)
env["LLM_BACKEND"] = "alibaba"
env["ALIBABA_LLM_ALLOW_LIVE_REQUESTS"] = "true"
# The judge stays local on purpose: a provider marking its own homework is not a
# measurement, and nothing about serving the UI changes that.
env["EVAL_JUDGE_LLM_BACKEND"] = "local"

# The interpreter that has the project's packages. The clone has no venv of its own —
# a second one would only be a second thing to keep in step.
python = Path(r"C:\Users\user\myUniproject\.venv\Scripts\python.exe")

print(f"serving {ROOT.name} on :{PORT} with LLM_BACKEND=alibaba (live requests OPEN)")
print("  .env is unchanged: local / false. This applies to this process only.\n")
raise SystemExit(
    subprocess.call(
        [str(python), "manage.py", "runserver", PORT, "--noreload"],
        cwd=str(ROOT),
        env=env,
    )
)
