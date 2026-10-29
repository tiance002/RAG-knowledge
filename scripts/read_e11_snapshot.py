"""Read a private E1.1 snapshot locally by its SHA-256 reference."""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.trace_schema import read_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha256', help='64-character lowercase SHA-256 snapshot reference')
    parser.add_argument('--root', default=os.environ.get('RAG_SNAPSHOT_DIR', './storage/observation_snapshots'),
                        help='private snapshot directory (defaults to RAG_SNAPSHOT_DIR)')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{64}', args.sha256):
        parser.error('snapshot reference must be a lowercase SHA-256, not a path')
    try:
        root = Path(args.root)
        if not root.is_dir():
            raise FileNotFoundError('snapshot directory not found')
        raw = read_snapshot(args.sha256, root=root)
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except (OSError, ValueError) as exc:
        print(f'snapshot unavailable: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
