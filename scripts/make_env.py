"""Create `.env` from `.env.example`, replacing every `change-me-*` value with a random secret."""

from __future__ import annotations

import re
import secrets
from pathlib import Path


def main() -> None:
    template = Path(".env.example").read_text()

    def secret(match: re.Match[str]) -> str:
        # Metabase requires a digit and mixed case; the prefix/suffix guarantee both.
        return f"Ddg-{secrets.token_urlsafe(18)}-7"

    Path(".env").write_text(re.sub(r"(?<==)change-me-[A-Za-z0-9-]+", secret, template))


if __name__ == "__main__":
    main()
