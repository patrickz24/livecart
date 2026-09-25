"""Container health probes, run by Docker ``HEALTHCHECK`` as ``livecart-healthcheck``.

The Python images ship neither curl nor wget. This command only uses the
standard library and never loads the settings, so it stays fast when Docker runs
it every few seconds, and it still answers when the configuration is broken.
Exit status: 0 healthy, 1 unhealthy, 2 usage error.
"""

import argparse
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence

HEALTHY, UNHEALTHY = 0, 1


def check_http(url: str, *, timeout_s: float) -> bool:
    """Return True when ``url`` answers with a 2xx status within ``timeout_s``."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http:// and https:// URLs can be probed")
    try:
        # The scheme is restricted above, so no file:// or custom opener is reachable.
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
            status: int = response.status
    except (urllib.error.URLError, OSError):
        # HTTPError (4xx, 5xx) is a URLError; refused connections and timeouts are OSErrors.
        return False
    return 200 <= status < 300


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="livecart-healthcheck", description=__doc__)
    probes = parser.add_subparsers(dest="probe", required=True)

    http = probes.add_parser("http", help="GET a URL and expect a 2xx status")
    http.add_argument("url", help="for example http://127.0.0.1:8000/health")
    http.add_argument("--timeout", type=float, default=3.0, help="seconds (default: 3)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        healthy = check_http(args.url, timeout_s=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))  # exits with status 2
    if not healthy:
        print(f"unhealthy: {args.url}", file=sys.stderr)
    return HEALTHY if healthy else UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
