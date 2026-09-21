"""Compatibility entry point: open Dashboard in the shared v4 host."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unified import main

if __name__ == '__main__':
    if '--view' not in sys.argv:
        sys.argv.extend(['--view', 'dashboard'])
    main()
