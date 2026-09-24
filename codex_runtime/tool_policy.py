"""Allow Codex's hosted web tool and deny other tool calls."""

import json
import sys


def _main() -> int:
    try:
        request = json.load(sys.stdin)
    except ValueError, UnicodeError:
        request = None

    # Codex 0.156.1 reports hosted search/open-page calls as `webrun` here.
    if isinstance(request, dict) and request.get("tool_name") == "webrun":
        return 0

    print("Local tools are disabled.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
