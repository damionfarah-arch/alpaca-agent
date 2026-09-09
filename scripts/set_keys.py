"""Interactive helper: paste your Alpaca keys when prompted, and this writes them
into .env. The secret is not echoed to the screen, not stored in shell history,
and not printed back.

    .venv/bin/python -m scripts.set_keys
"""

from __future__ import annotations

import getpass
import re
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
EXAMPLE = ENV.parent / ".env.example"


def _set(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
        return re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.M)
    return text.rstrip() + "\n" + line + "\n"


def main() -> int:
    if not ENV.exists():
        if EXAMPLE.exists():
            ENV.write_text(EXAMPLE.read_text())
            print(f"created {ENV.name} from .env.example")
        else:
            ENV.write_text("")

    print("\nPaste your Alpaca PAPER API keys.")
    print("Get them at https://app.alpaca.markets  ->  switch to 'Paper' (top-left)")
    print("->  'Generate API Keys'. The secret is shown only once.\n")

    key_id = input("  API Key ID:  ").strip()
    secret = getpass.getpass("  API Secret:  (hidden) ").strip()

    if not key_id or not secret:
        print("\nboth values are required -- nothing written.")
        return 1

    text = ENV.read_text()
    text = _set(text, "ALPACA_API_KEY", key_id)
    text = _set(text, "ALPACA_SECRET_KEY", secret)
    # make sure we stay on paper + shadow for now
    text = _set(text, "ALPACA_ENV", "paper")
    text = _set(text, "EXECUTION_MODE", "shadow")
    ENV.write_text(text)

    try:
        ENV.chmod(0o600)
    except OSError:
        pass

    masked = key_id[:4] + "…" + key_id[-2:] if len(key_id) > 6 else "set"
    print(f"\nwrote ALPACA_API_KEY ({masked}) and ALPACA_SECRET_KEY to {ENV.name}")
    print("ALPACA_ENV=paper, EXECUTION_MODE=shadow  (unchanged)")
    print("\nnext:  .venv/bin/python -m scripts.smoke_test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
