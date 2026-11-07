"""Compare real PDF parser routes offline; no ingestion, index or provider calls.

Manifest: {"cases": [{"id": "example", "path": "/absolute/source.pdf",
                       "sha256": "optional frozen source hash"}]}.
Run under the project's existing backend Python environment. Outputs contain
source-derived text and belong in private local storage, never in Git.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    default=str), encoding='utf-8')


def disable_network():
    # Guard the comparison itself against accidental remote model/exporter use.
    # OCR subprocesses use local Tesseract only; no shell scripts are invoked.
    import socket
    attempts = []
    def refused(*args, **kwargs):
        attempts.append('NETWORK_CALL_BLOCKED')
        raise RuntimeError('NETWORK_CALL_BLOCKED: offline PDF route comparison')
    socket.socket.connect = refused
    socket.socket.connect_ex = refused
    socket.create_connection = refused
    return attempts


def worker(args):
    target = Path(args.worker_out).resolve()
    target.mkdir(parents=True, exist_ok=False)
    os.environ.update(RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false',
                      RAG_PROMETHEUS_ENABLED='false', PDF_PARSER_MODE=args.route,
                      IMAGE_DIR=str(target / 'images'),
                      PDF_AUDIT_DIR=str(target / 'manifests'))
    sys.path.insert(0, str(Path(args.backend).resolve()))
    attempts = disable_network()
    started = time.monotonic()
    result = {'route': args.route, 'status': 'FAILED', 'provider_calls': 0,
              'answer_accuracy': 'NOT_RUN', 'retrieval_recall': 'NOT_RUN'}
    try:
        from app.core.config import get_settings
        settings = get_settings()
        # Assign only experiment-local cached settings; each arm is a process.
        settings.PDF_PARSER_MODE = args.route
        settings.IMAGE_DIR = str(target / 'images')
        settings.PDF_AUDIT_DIR = str(target / 'manifests')
        settings.PDF_SCANNED_TABLES_ENABLED = args.scan_tables
        if args.tessdata:
            settings.PDF_TESSDATA_DIR = str(Path(args.tessdata).resolve())
        from app.services.parser import FileParser
        from app.services.pdf_pipeline import image_coverage
        import fitz
        source = Path(args.source).resolve(strict=True)
        result.update(source_sha256=digest(source), source_bytes=source.stat().st_size,
                      parser_sha256=digest(Path(args.backend)/'app/services/parser.py'),
                      pipeline_sha256=digest(Path(args.backend)/'app/services/pdf_pipeline.py'),
                      python=sys.version, pymupdf=fitz.VersionBind,
                      configuration={'chunk_mode':'custom','chunk_size':500,
                                     'chunk_overlap':50,'use_semantic':False,
                                     'scan_tables':args.scan_tables,
                                     'ocr_dpi':settings.PDF_OCR_DPI,
                                     'tessdata_exists':Path(settings.PDF_TESSDATA_DIR).is_dir()})
        result['dependencies'] = {}
        for package in ('PyMuPDF','pandas','python-docx','langchain-experimental','langchain-text-splitters'):
            try:
                result['dependencies'][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                result['dependencies'][package] = 'NOT_INSTALLED'
        pages = []
        with fitz.open(source) as document:
            for page in document:
                native = page.get_text('text')
                pages.append({'page':page.number+1,'native_chars':len(native.strip()),
                              'native_text_sha256':hashlib.sha256(native.encode()).hexdigest(),
                              'image_count':len(page.get_images()),
                              'image_coverage':image_coverage(page)})
        parser = FileParser('offline-compare')
        parse_started = time.monotonic()
        chunks = parser.parse(str(source), chunk_mode='custom', chunk_size=500,
                              chunk_overlap=50, use_semantic=False)
        result['parse_elapsed_s'] = round(time.monotonic()-parse_started,3)
        result['source_sha256_after'] = digest(source)
        if result['source_sha256_after'] != result['source_sha256']:
            raise RuntimeError('Input source changed during parsing')
        rows = [dataclasses.asdict(chunk) for chunk in chunks]
        write_json(target/'chunks.json', rows)
        text = '\n\n'.join(row['content'] for row in rows)
        (target/'extracted.txt').write_text(text,encoding='utf-8')
        for page in pages:
            anchored = [r for r in rows if r.get('page_number') == page['page']]
            # An expanded chunk may span pages. Do not invent exact per-page text.
            page['anchored_chunk_count'] = len(anchored)
            page['anchored_chars'] = sum(len(r['content']) for r in anchored)
            page['anchored_table_chunks'] = sum(r['content_type']=='table' for r in anchored)
            page['anchored_image_chunks'] = sum(r['content_type']=='image' for r in anchored)
        result.update(status='COMPLETED', page_count=len(pages), pages=pages,
                      chunk_count=len(rows), text_chars=len(text),
                      text_utf8_bytes=len(text.encode()),
                      text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                      table_chunks=sum(r['content_type']=='table' for r in rows),
                      image_chunks=sum(r['content_type']=='image' for r in rows),
                      source_located_chunks=sum(bool((r.get('metadata') or {}).get('source_locator')) for r in rows),
                      page_records=getattr(parser,'pdf_page_records',None),
                      parse_manifest=getattr(parser,'pdf_manifest_path',None))
        if attempts:
            raise RuntimeError('Route attempted network access; comparison invalid')
    except BaseException as exc:
        result.update(status='FAILED', error_type=type(exc).__name__, error=str(exc),
                      traceback=traceback.format_exc())
    result.update(elapsed_s=round(time.monotonic()-started,3),
                  network_attempts=len(attempts))
    write_json(target/'result.json',result)
    return 0 if result['status']=='COMPLETED' else 1


def main(args):
    manifest = Path(args.manifest).resolve(strict=True)
    frozen = json.loads(manifest.read_text(encoding='utf-8'))
    cases = frozen['cases']
    if not cases or len(cases)>20:
        raise ValueError('Use 1 to 20 frozen PDF cases')
    seen = set()
    for case in cases:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}',case['id']) or case['id'] in seen:
            raise ValueError('Case ID must be unique and path-safe')
        seen.add(case['id'])
        source=Path(case['path']).resolve(strict=True)
        if source.suffix.lower()!='.pdf': raise ValueError('PDF input required')
        actual=digest(source)
        if case.get('sha256') and case['sha256']!=actual: raise ValueError('Frozen source hash mismatch: '+case['id'])
        case['path']=str(source);case['actual_sha256']=actual
    out = Path(args.out).resolve()
    out.mkdir(parents=True,exist_ok=False)
    write_json(out/'inputs.json', {'manifest_sha256':digest(manifest),
                                 'runner_sha256':digest(__file__),'cases':cases})
    records=[]
    for case in cases:
        for route in ('legacy','enhanced'):
            arm=out/(case['id']+'-'+route)
            cmd=[sys.executable,str(Path(__file__).resolve()),'--worker','--backend',args.backend,
                 '--source',case['path'],'--route',route,'--worker-out',str(arm)]
            if args.scan_tables:cmd.append('--scan-tables')
            if args.tessdata:cmd.extend(['--tessdata',args.tessdata])
            log=out/(case['id']+'-'+route+'.log')
            with log.open('wb') as handle:
                try:
                    run=subprocess.run(cmd,stdout=handle,stderr=subprocess.STDOUT,timeout=args.timeout)
                    record={'id':case['id'],'route':route,'exit_code':run.returncode}
                except subprocess.TimeoutExpired:
                    record={'id':case['id'],'route':route,'exit_code':None,'status':'UNKNOWN','reason':'LOCAL_PARSE_TIMEOUT'}
            if (arm/'result.json').exists():record.update(json.loads((arm/'result.json').read_text(encoding='utf-8')))
            elif record.get('status')!='UNKNOWN':record.update(status='FAILED',reason='NO_WORKER_RECEIPT')
            record['source_sha256_verified']=record.get('source_sha256')==case['actual_sha256']
            records.append(record)
            write_json(out/'results.json', records)
    lines=['# PDF route comparison','',
           'Real parser output, no provider calls. Character/chunk counts measure extraction, not answer accuracy.',
           'Gold quality, retrieval recall, generation latency and billed cost: NOT_RUN.', '',
           '| Case | Route | Status | Exit | Chunks | Characters | UTF-8 bytes | Tables | Located | Parse seconds |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in records:
        lines.append('| '+' | '.join(str(r.get(k,'UNKNOWN')) for k in ['id','route','status','exit_code','chunk_count','text_chars','text_utf8_bytes','table_chunks','source_located_chunks','parse_elapsed_s'])+' |')
    lines+=['','For each arm read `extracted.txt`, `chunks.json`, `result.json` and its log. Larger text is not necessarily better; image/OCR/table duplication requires source review.',
            'Per-page anchored character counts are not exact page attribution for chunks spanning multiple pages. Text counts include chunk overlap and image markers; they are not unique recovered source characters. Parse seconds exclude imports and common source inspection, while result elapsed_s includes worker setup. Source PDFs are read-only inputs.']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return 0 if all(r.get('status')=='COMPLETED' and r['source_sha256_verified'] for r in records) else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest')
    parser.add_argument('--out')
    parser.add_argument('--backend',default=str(Path(__file__).resolve().parents[1]/'backend'))
    parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--tessdata')
    parser.add_argument('--scan-tables',action='store_true')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--source',help=argparse.SUPPRESS)
    parser.add_argument('--route',choices=['legacy','enhanced'],help=argparse.SUPPRESS)
    parser.add_argument('--worker-out',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args.worker and (not args.manifest or not args.out):parser.error('--manifest and --out required')
    raise SystemExit(worker(args) if args.worker else main(args))
