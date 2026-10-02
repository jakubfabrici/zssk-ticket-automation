import sys
from pathlib import Path

# Tests run from anywhere; the modules live one directory up (kit root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
