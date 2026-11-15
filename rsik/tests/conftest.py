"""Register portable synthetic fixtures for source-checkout tests."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from portable_fixtures import synthetic_split, synthetic_replay
