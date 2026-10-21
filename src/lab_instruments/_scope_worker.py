"""Private watchdog worker; no additional SCPI interface."""

import sys
from pathlib import Path

from .sds import load_plan, perform

if __name__ == "__main__":
    directory = Path(sys.argv[1])
    plan, _, _ = load_plan(directory / "plan.json", sys.argv[2])
    perform(plan, directory)
