"""Create `.env` from `.env.example`, replacing every `change-me-*` value with a random secret.

python3 scripts/make_env.py            # login off (local use)
python3 scripts/make_env.py --auth     # also enable the UI/API login with a generated password
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path


def _secret() -> str:
    # Metabase requires a digit and mixed case; the prefix/suffix guarantee both.
    return f"Ddg-{secrets.token_urlsafe(18)}-7"


def main(argv: list[str]) -> None:
    text = re.sub(
        r"(?<==)change-me-[A-Za-z0-9-]+", lambda _: _secret(), Path(".env.example").read_text()
    )
    if "--auth" in argv:
        password = _secret()
        text = re.sub(r"(?m)^AUTH_USERNAME=.*$", "AUTH_USERNAME=admin", text)
        text = re.sub(r"(?m)^AUTH_PASSWORD=.*$", f"AUTH_PASSWORD={password}", text)
        print("UI/API login enabled: user 'admin', password in .env (AUTH_PASSWORD)")
    Path(".env").write_text(text)


if __name__ == "__main__":
    main(sys.argv[1:])
