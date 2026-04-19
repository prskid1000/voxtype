"""Allow `python -m voxtype` to launch the app."""
from voxtype.main import main

if __name__ == "__main__":
    raise SystemExit(main())
