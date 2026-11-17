"""
build_function.py

Copies the shared pipeline/ and model/ packages into function_app/ so the function deploys as one folder,
and refreshes function_app/requirements.txt from the root one. The copies are gitignored.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "function_app"
PACKAGES = ["pipeline", "model"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "artifacts")


def main():
    for name in PACKAGES:
        dest = TARGET / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(ROOT / name, dest, ignore=SKIP)
        print(f"copied {name}/ -> function_app/{name}/")
    shutil.copyfile(ROOT / "requirements.txt", TARGET / "requirements.txt")
    print("copied requirements.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
