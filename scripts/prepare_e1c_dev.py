"""Audit approved historical local artifacts without replaying questions.

This preparation-only command deliberately cannot issue a READY manifest while
independent file-to-KB ownership is absent. It imports no application/runner,
opens SQLite in immutable read-only mode, and never executes historical scripts.
Private per-member evidence is kept under ignored var/r1; the public report
contains hashes, counts and blockers only. No citation is an ownership oracle.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = (
    'pdf-table-hardcases-20261027', 'p03-normal-zero-report',
    'p1-regression-stage-20261029', 'p0-observability', 'e1-20261029',
    'pdf-ab-20261027-final', 'review250-20261028',
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf8'))


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def mapping_records(value, file_ids, keypath=''):
    """Locate explicit co-occurring IDs; matches still require provenance review."""
    if isinstance(value, dict):
        kb = next((value[k] for k in ('kb_id', 'knowledge_base_id', 'kbId') if value.get(k)), None)
        file_id = next((value[k] for k in ('file_id', 'fileId', 'id') if
                        isinstance(value.get(k), str) and value[k] in file_ids), None)
        if kb and file_id:
            yield {'keypath': keypath, 'file_id': file_id, 'kb_id': kb,
                   'fields': sorted(value),
                   'classification': ('DERIVED_RETRIEVAL_CITATION' if
                       any(word in keypath for word in ('final_citations', 'candidate', 'retrieval'))
                       else 'REQUIRES_INDEPENDENT_PROVENANCE_REVIEW')}
        for key, child in value.items():
            yield from mapping_records(child, file_ids, keypath + '/' + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from mapping_records(child, file_ids, keypath + '/' + str(index))


def inventory(approved, file_ids):
    records, failures, mappings, databases, archives = [], [], [], [], []
    for dirname in SEARCH_ROOTS:
        for path in sorted((approved / dirname).rglob('*')):
            if path.suffix not in ('.json', '.jsonl'):
                continue
            try:
                data = path.read_bytes()
                values = ([json.loads(line) for line in data.decode('utf-8-sig').splitlines() if line.strip()]
                          if path.suffix == '.jsonl' else [json.loads(data.decode('utf-8-sig'))])
                hits = [hit for value in values for hit in mapping_records(value, file_ids)]
                records.append({'path': str(path.relative_to(approved)), 'sha256': sha(data),
                                'explicit_mapping_count': len(hits)})
                mappings.extend({'path': str(path.relative_to(approved)), **hit} for hit in hits)
            except (ValueError, UnicodeError, OSError) as exc:
                failures.append({'path': str(path.relative_to(approved)), 'error': type(exc).__name__})
    for path in sorted(approved.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix in ('.db', '.sqlite', '.sqlite3'):
            try:
                with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
                    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                databases.append({'path': str(path.relative_to(approved)), 'tables': tables,
                                  'has_file_documents': 'file_documents' in tables})
            except sqlite3.Error as exc:
                failures.append({'path': str(path.relative_to(approved)), 'error': type(exc).__name__})
        elif path.suffix in ('.tgz', '.tar', '.zip') or path.name.endswith('.tar.gz'):
            try:
                if path.suffix == '.zip':
                    with zipfile.ZipFile(path) as archive:
                        names = archive.namelist()
                else:
                    with tarfile.open(path) as archive:
                        names = archive.getnames()
                archives.append({'path': str(path.relative_to(approved)), 'sql_members': [
                    name for name in names if name.lower().endswith(('.db', '.sqlite', '.sqlite3'))]})
            except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
                failures.append({'path': str(path.relative_to(approved)), 'error': type(exc).__name__})
    return {'json_records': records, 'explicit_mappings': mappings, 'databases': databases,
            'archives': archives, 'errors': failures}


def audit(approved, private_out, public_out):
    approved = approved.resolve()
    if not approved.is_dir():
        raise ValueError('approved historical artifact root does not exist')
    private_out = private_out.resolve()
    private_out.relative_to((ROOT / 'var/r1').resolve())
    if private_out.exists() or public_out.exists():
        raise FileExistsError('audit outputs must be new; retain original evidence')
    refs = {'snapshot': 'e20-20261030/research-frozen-contexts.json',
            'labels': 'e2-1/member-quality-ledger.json',
            'source_audit': 'e2-1/source-tokenizer-audit-ecs.json',
            'source_audit_implementation': 'e2-1/source-tokenizer-audit-ecs.py',
            'label_manifest': 'e2-1/stage1-manifest.json'}
    inputs = {key: {'path': str(approved / rel), 'raw_sha256': sha((approved / rel).read_bytes())}
              for key, rel in refs.items()}
    frozen, labels, source_audit = (load(approved / refs[k]) for k in ('snapshot', 'labels', 'source_audit'))
    chunks = {row['id']: row for row in frozen['chunks']}
    if len(chunks) != len(frozen['chunks']):
        raise ValueError('duplicate frozen chunk ID')
    if any(sha(c['content'].encode('utf8')) != c['content_sha256'] for c in chunks.values()):
        raise ValueError('historical content hash mismatch')
    if fingerprint(frozen['chunks']) != frozen['actual_snapshot_chunk_subset_fingerprint']:
        raise ValueError('historical snapshot subset fingerprint mismatch')
    source_files = source_audit['files']
    file_ids = {c['file_id'] for c in chunks.values()}
    index_checks = {row['member_id']: row['index_content_match'] for row in source_audit['member_checks']}
    gold_checks = {row['qid']: row for row in source_audit['gold_checks']}
    rows, detail = [], []
    for question in frozen['questions']:
        qid = question['qid']
        annotations = [r for r in labels if r['qid'] == qid]
        alignment = []
        for row in annotations:
            chunk = chunks.get(row['member_id'], {})
            source = source_files.get(row['source_file_id'], {})
            loc = row['source_location']
            identity = (chunk.get('file_id') == row['source_file_id'] and
                        chunk.get('content_sha256') == row['content_hash'] and
                        source.get('sha256') == row['source_file_hash'] and
                        source.get('matches_declared_file_id') is True and
                        index_checks.get(row['member_id']) is True)
            indexed_loc = (chunk.get('original_index') == loc.get('original_index') and
                           chunk.get('page_number') == loc.get('indexed_page_number'))
            # The Office parser's indexed page is NOT a verified Word page/cell.
            gold = gold_checks.get(qid, {})
            verified_gold_loc = (row['source_location_status'] == 'GOLD_LOCATION_VERIFIED' and
                gold.get('source_hash_match') is True and gold.get('gold_value_match') is True and
                all(loc.get(k) == value for k, value in gold.get('gold_location_checked', {}).items()
                    if k != 'sha256') and
                gold.get('gold_location_checked', {}).get('sha256') == row['source_file_hash'])
            alignment.append({'member_id': row['member_id'], 'source_file_id': row['source_file_id'],
                'content_sha256': row['content_hash'], 'source_location': loc,
                'identity_source_hash_match': identity, 'indexed_location_match': indexed_loc,
                'historical_gold_location_crosscheck': verified_gold_loc,
                'label_status': row['label_status'], 'review_status': row['review_status'],
                'structure_necessity': row['structure_necessity']})
        used_files = {chunks[r['member_id']]['file_id'] for r in annotations}
        rows.append({'qid': qid, 'candidate_seeds': len(question['candidates']),
            'member_occurrences': len(annotations), 'unique_members': len({r['member_id'] for r in annotations}),
            'files_needing_independent_ownership': len(used_files), 'verified_ownership_files': 0,
            'identity_source_hash_aligned': sum(r['identity_source_hash_match'] for r in alignment),
            'indexed_location_aligned': sum(r['indexed_location_match'] for r in alignment),
            'historical_gold_location_crosschecked_occurrences': sum(r['historical_gold_location_crosscheck'] for r in alignment),
            'historical_gold_location_crosschecked_unique': len({r['member_id'] for r in alignment if r['historical_gold_location_crosscheck']}),
            'location_status': dict(Counter(r['source_location_status'] for r in annotations)),
            'label_status': dict(Counter(r['label_status'] for r in annotations)),
            'structure_necessity': dict(Counter(r['structure_necessity'] for r in annotations)),
            'status': 'BLOCKED'})
        detail.append({'qid': qid, 'alignment': alignment})
    scanned = inventory(approved, file_ids)
    source_issues = []
    independent_candidates = [m for m in scanned['explicit_mappings'] if
                              m['classification'] != 'DERIVED_RETRIEVAL_CITATION']
    if independent_candidates or any(r['has_file_documents'] for r in scanned['databases']):
        source_issues.append('Additional ownership candidates exist; manual provenance review required before READY')
    synthetic = load(ROOT / 'experiments/r0/synthetic.json')
    b = dict(synthetic['arms']['B'])
    b.update(path='../../experiments/r0/exact-duplicate.json',
             raw_sha256=sha((ROOT / 'experiments/r0/exact-duplicate.json').read_bytes()),
             worker_path='scripts/rsi_delivery_worker.py',
             worker_raw_sha256=sha((ROOT / 'scripts/rsi_delivery_worker.py').read_bytes()))
    report = {'experiment_id': 'r1-e1c-dev-readiness', 'status': 'BLOCKED',
        'execution_status': 'NOT_RUN', 'evaluation_scope': 'DEV_ONLY', 'holdout': 'NOT_PREPARED',
        'cases': {'path': None, 'sha256': None}, 'evaluation': {'path': None, 'sha256': None},
        'base_commit': synthetic['base_commit'], 'fixed': synthetic['fixed'],
        'candidate': b, 'inputs': inputs, 'questions': rows,
        'summary': {'questions': len(rows), 'candidate_seeds': sum(r['candidate_seeds'] for r in rows),
                    'snapshot_chunks': len(chunks), 'snapshot_files': len(file_ids),
                    'member_occurrences': len(labels), 'independent_ownership_files': 0,
                    'json_files_scanned': len(scanned['json_records']),
                    'explicit_mapping_candidates': len(scanned['explicit_mappings']),
                    'derived_retrieval_citations': sum(m['classification'] == 'DERIVED_RETRIEVAL_CITATION' for m in scanned['explicit_mappings']),
                    'independent_mapping_candidates': len(independent_candidates),
                    'sql_databases_checked': len(scanned['databases']),
                    'sql_with_file_documents': sum(d['has_file_documents'] for d in scanned['databases']),
                    'archives_checked': len(scanned['archives']),
                    'archive_sql_members': sum(len(a['sql_members']) for a in scanned['archives']),
                    'scan_errors': len(scanned['errors'])},
        'candidate_applicability': {'selection': 'exact_duplicate', 'new_policy': False,
            'required_metadata': 'PDF filename; page>0; four-number bbox; nonempty parent_id and heading_text; exact file/KB/content/locator match; never remove independent seed',
            'historical_formats': dict(Counter(Path(source_files[f]['path']).suffix for f in file_ids)),
            'non_null_parent_id': sum(c['parent_id'] is not None for c in chunks.values()),
            'nonempty_heading_text': sum(bool(c['heading_text']) for c in chunks.values()),
            'snapshot_source_locator_fields': sum('source_locator' in c for c in chunks.values()),
            'eligible_members': 0,
            'conclusion': '历史 DOCX/XLSX 不满足现有 PDF 去重门槛；仅有跨 ID 同文不构成可删证明。'},
        'blockers': [
            {'id': 'OWNERSHIP', 'missing': '10 个文件均缺少独立、与历史版本绑定的 file→KB 归属记录；检索 citation 的 kb_id 不可补作 FileDocument。',
             'remedy': '提供已获准的历史隔离 SQL 只读快照或独立文件/KB 导出，记录 DB/导出哈希、file ID、KB ID、文件名、来源文件哈希，并核对 72 个成员内容及索引位置。'},
            {'id': 'EVALUATION', 'missing': '281 次成员标注均为 AI_PROPOSED；结构必要性和多数实际位置仍未知，尚无独立冻结评分文件。',
             'remedy': '逐题审核必要事实、字段/表头/单位及其真实文档位置；以 source hash+locator+exact content hash 对齐后单独冻结 evaluator，保留 UNKNOWN。'},
            {'id': 'APPLICABILITY', 'missing': '当前 B 只支持具备严格定位证据的 PDF；72 个历史 Office chunk 无 parent_id/heading_text/source_locator。',
             'remedy': '保持现有候选身份可做无删减控制；若需 Office 选择规则，另开候选版本并独立评审，不能在本次就绪整理中静默改变 B。'},
            {'id': 'HOLDOUT', 'missing': '六题均已开发使用；独立留出集尚未准备。',
             'remedy': '另选未用于设计/调参的授权数据，开发集和留出集分别报告；当前最多只能 READY_DEV_ONLY。'}],
        'additional_review': source_issues,
        'boundary': {'historical_replays': 0, 'external_model_calls': 0, 'ecs_access': 0,
                     'production_access': 0, 'copies_of_corpus_or_index': 0},
        'private_evidence': str(private_out / 'alignment-and-inventory.json'),
        'original_plan': {'path': 'experiments/r0/e1c-planned.json',
                          'raw_sha256': sha((ROOT / 'experiments/r0/e1c-planned.json').read_bytes()),
                          'status': load(ROOT / 'experiments/r0/e1c-planned.json')['status']},
        'note': '此文件为就绪审计结果，非可执行 READY 清单；未执行历史题回放、质量/收益或晋级判定。'}
    private_out.mkdir(parents=True)
    write(private_out / 'alignment-and-inventory.json', {'inputs': inputs, 'questions': detail, 'inventory': scanned})
    write(public_out, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-var', required=True, type=Path)
    parser.add_argument('--private-out', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.approved_var, args.private_out, args.report)
    print(json.dumps({'status': result['status'], 'execution_status': result['execution_status'],
                      'summary': result['summary']}, ensure_ascii=False))
