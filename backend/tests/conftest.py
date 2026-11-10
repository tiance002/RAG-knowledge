from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Several offline fixtures register a substitute if Chroma has not been imported.
# Load the installed package before collection so real-vector integration tests
# exercise the same client regardless of the order in which modules are collected.
try:
    import chromadb
except ModuleNotFoundError as exc:
    if exc.name != "chromadb":
        raise
