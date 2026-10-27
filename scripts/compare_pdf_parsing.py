"""Paired parsing on identical source bytes; never calls an LLM or Embedding."""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.parser import FileParser, settings
from app.services.pdf_pipeline import image_coverage
import fitz


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline-parser', required=True)
    ap.add_argument('--corpus', required=True)
    ap.add_argument('--tessdata', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--scan-tables', action='store_true')
    ap.add_argument('--table-ablation',action='store_true',help='Same enhanced parser, table structure off/on')
    args = ap.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit('Output already exists; preserve prior attempts')
    output.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location('frozen_ecs_parser', args.baseline_parser)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    frozen = {'chunk_mode': 'custom', 'chunk_size': 500, 'chunk_overlap': 50,
              'use_semantic': False, 'embedding_model': None}
    settings.PDF_TESSDATA_DIR = str(Path(args.tessdata).resolve())
    settings.PDF_PARSER_MODE = 'enhanced'
    settings.PDF_SCANNED_TABLES_ENABLED = args.scan_tables
    settings.PDF_AUDIT_DIR = str(output / 'manifests')
    summary = {'baseline_parser_sha256': hashlib.sha256(Path(args.baseline_parser).read_bytes()).hexdigest(),
               'pymupdf': fitz.VersionBind, 'split_config': frozen, 'scan_tables': args.scan_tables,
               'model_calls': 0, 'sources': [],
               'candidate_source_hashes':{str(p.name):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (Path(__file__).resolve().parents[1]/'backend/app/services').glob('pdf_*.py')}}
    summary['comparison']='same_v2_tables_off_on' if args.table_ablation else 'legacy_vs_enhanced'
    if args.table_ablation:summary['baseline_parser_sha256']=None
    for path in sorted(Path(args.corpus).glob('*.pdf')):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stem != sha:
            raise ValueError('Corpus SHA filename mismatch')
        source = {'source_sha256': sha, 'bytes': path.stat().st_size, 'arms': {}}
        for arm, parser_type in [('before', FileParser if args.table_ablation else baseline.FileParser), ('after', FileParser)]:
            settings.PDF_TABLES_ENABLED=(arm=='after') if args.table_ablation else True
            settings.IMAGE_DIR = str(output / arm / 'images')
            parser = parser_type('pdf-ab')
            start = time.monotonic()
            chunks = parser.parse(str(path), **frozen)
            records = getattr(parser, 'pdf_page_records', None)
            if records is None:
                with fitz.open(path) as doc:
                    records = [dict(page=p.number+1, text_chars=len(p.get_text('text').strip()),
                                    image_coverage=image_coverage(p), route='native-blocks-only') for p in doc]
            data = {'source_sha256': sha, 'chunks': [asdict(c) for c in chunks], 'pages': records}
            dest = output / arm / (sha + '.json')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            source['arms'][arm] = {'chunks': len(chunks), 'types': dict(Counter(c.content_type for c in chunks)),
                'text_chars': sum(len(c.content) for c in chunks if c.content_type != 'image'),
                'pages': len(records), 'routes': dict(Counter(r['route'] for r in records)),
                'seconds': round(time.monotonic()-start, 3), 'output': str(dest)}
            print(json.dumps({'source': sha, 'arm': arm, **source['arms'][arm]}), flush=True)
        summary['sources'].append(source)
        (output/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
