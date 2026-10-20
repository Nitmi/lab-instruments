"""Private worker entry point, launched by the public capture supervisor."""

import argparse
from pathlib import Path

from .capture import worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "sdk", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--sdk-sha256", required=True)
    args = parser.parse_args()
    return worker(args.plan, args.sha256, args.sdk, args.sdk_sha256, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
