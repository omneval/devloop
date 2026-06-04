import sys
from pathlib import Path

# Add repo root to sys.path so scripts/ and src/ are importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
