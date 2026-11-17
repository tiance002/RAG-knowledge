"""Read-only RSI analytics CLI. Never invokes agents, registries or evaluators."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .reader import canonical, digest, read_artifact, read_event_log
from .projections import DIMENSIONS, project_sources
from .metrics import compute_metrics
from .schemas import GROUP_DIMENSIONS, METRICS


CAPABILITIES = [
    ('EventLog', '已实现', 'events.py:EventLog; analytics.reader.read_event_log', '验证链与文件哈希；无可信锚点不能证明历史尾部完整'),
    ('六个 Agent', '部分实现', 'openai_roles.py:OpenAIRoles.call', '传输记录不能证明输出契约有效，缺统一语义 invocation 事件'),
    ('Proposal Portfolio', '已实现', 'analytics.portfolio.analyze_portfolio', '仅比较已保存且可验证的策略身份，不猜测自然语言等价'),
    ('Query Workflow', '已实现', 'query_workflow.py:run_query_round', '主要身份来自归档 round，事件源覆盖不完整'),
    ('Registry', '已实现', 'query_aware.py:QueryRegistry', '只读已保存不可变身份；不以当前快照重建历史'),
    ('FamilyDecision', '已实现', 'family_decision.py:make_family', '完整三槽与终态资格必须显式存在'),
    ('Memory', '部分实现', 'query_learning.py; handbook/store.py', '原始检索次数、共同检索集合和完整冲突分母缺失'),
    ('Parent Selection', '数据不足', 'lineage.py:ParentPool.select', '选择返回未写事件，不能把父代准入当选择'),
    ('Replay', '已实现', 'holdout.py:replay_artifacts', '真实资格、配对、完成、版本缺一不可，演练单列'),
    ('Holdout', '已实现', 'query_verification.py:verify_sealed', '仅投影授权终态汇总，不读取封闭题目/答案/证据'),
    ('Promotion', '仅有接口', 'promotion.py:PromotionSandbox', '现有实现为隔离演练，不证明生产批准或发布'),
    ('Recovery', '部分实现', 'query_learning.py:ExperienceSession', '最终状态文件不构成所有恢复尝试分母'),
    ('Cost', '部分实现', 'budgets.py; analytics.metrics', 'usage、上界、UNKNOWN 与实际供应商账单分开'),
    ('任意系统源码自改', '尚未实现', 'M0 未新增权限', 'M0 只新增分析代码'),
]

GAPS = [
    '未记录的维度保持 UNKNOWN；不同模式、Evaluator、Profile 和兼容指纹不合并。',
    'MODEL_REQUEST_COMPLETED 是传输回执，不是 Schema 成功；训练 ADOPT 不等于独立 VERIFIED。',
    'Memory matched 是已归档最终匹配列表；不伪造原始 retrieval 和共同检索对分母。',
    '历史实际费用缺供应商账单时 UNKNOWN；账本上界不等于实扣金额。',
    '当前 Registry tip 不用于推断历史选择、资格或回滚状态。',
]


def filter_records(records, filters=None):
    filters = filters or {}
    unknown = set(filters) - set(DIMENSIONS)
    if unknown:
        raise ValueError('ANALYTICS_UNKNOWN_FILTER')
    return [r for r in records if all(str(r['dimensions'].get(k, 'UNKNOWN')) == str(v)
                                    for k, v in filters.items() if v is not None)]


def build_report(paths, artifact_specs=(), filters=None, expected_hashes=None):
    """Validate all input chains before filtering and retain source provenance."""
    expected_hashes = expected_hashes or {}
    sources = [read_event_log(p, expected_hashes.get(str(Path(p).resolve()))) for p in paths]
    artifacts = [read_artifact(s['path'], s['sha256'], s['kind']) for s in artifact_specs]
    projected = project_sources(sources, artifacts)
    records = filter_records(projected, filters)
    statistics = compute_metrics(records)
    # Verify sources again after projection, detecting concurrent external changes.
    for source in sources:
        read_event_log(source['path'], source['sha256'])
    for spec in artifact_specs:
        read_artifact(spec['path'], spec['sha256'], spec['kind'])
    invocations = [r for r in records if r['projection_type'] == 'AgentInvocation']
    transport = Counter(str(r['payload'].get('transport_status', 'UNKNOWN')) for r in invocations)
    known = lambda value: value not in (None, '', 'UNKNOWN')
    search = []
    for group in statistics['groups']:
        members = [r for r in records if all(r['dimensions'].get(k, 'UNKNOWN') == group['dimensions'][k] for k in GROUP_DIMENSIONS)
                   and r['payload'].get('metric_contract_version', 'LEGACY') == group.get('metric_contract_version', 'LEGACY')]
        edges = sorted({(r['dimensions'].get('parent_id'), r['dimensions'].get('candidate_id'))
                        for r in members if known(r['dimensions'].get('parent_id')) and known(r['dimensions'].get('candidate_id'))})
        round_parents = Counter(r['dimensions']['parent_id'] for r in members
                                if r['projection_type'] == 'Round' and known(r['dimensions'].get('parent_id')))
        search.append({'dimensions': group['dimensions'], 'observed_edges': [{'parent_id': a, 'candidate_id': b} for a, b in edges],
                       'status': 'OBSERVED_PARTIAL' if edges else 'UNKNOWN',
                       'observed_round_parent_counts': dict(sorted(round_parents.items())),
                       'selection_count_status': 'UNKNOWN'})
        if 'metric_contract_version' in group:
            search[-1]['metric_contract_version'] = group['metric_contract_version']
    report = {'schema': 'rsi-analytics-report/v1', 'metrics_schema': 'rsi-metrics/v1',
            'source_fingerprint': digest(sorted(set(s['sha256'] for s in sources + artifacts))),
            'sources': sorted([{k: s[k] for k in ('source_id', 'path', 'sha256')} for s in sources + artifacts],
                              key=lambda s: (s['source_id'], s['path'])),
            'filters': {k: v for k, v in sorted((filters or {}).items()) if v is not None},
            'projection_count_before_filter': len(projected), 'projection_count': len(records),
            'projection_types': dict(sorted(Counter(r['projection_type'] for r in records).items())),
            'statistics': statistics, 'transport_status_distribution': dict(sorted(transport.items())),
            'search_tree': {'groups': search,
                            'limitation': 'Archived round parents are a coverage-limited proxy, not all parent selection events.'},
            'failure_origin_unknown_count': sum(r['payload'].get('failure_origin', 'UNKNOWN') == 'UNKNOWN' for r in invocations),
            'capabilities': [{'module': a, 'status': b, 'entrypoint': c, 'gap': d} for a, b, c, d in CAPABILITIES],
            'evidence_gaps': GAPS, 'projections': records,
            'next_stage': {'implemented': False, 'name': 'M1',
                           'profiles': ['M1_SPECIALIZED_V1', 'M1_INDEPENDENT_V1', 'M1_ADAPTIVE_V1', 'M1_EXPERIENCE_GUIDED_V1'],
                           'recommendation': '先补齐缺失的调用/检索/父代选择记录契约，再经人工审查冻结对照实验；不覆盖 LEGACY。'}}
    contracts = sorted({g['metric_contract_version'] for g in statistics['groups'] if g.get('metric_contract_version')})
    if contracts:
        report['metric_contract_versions'] = contracts
        report['metrics_schema'] = contracts[0] if len(contracts) == 1 and all(g.get('metric_contract_version') for g in statistics['groups']) else 'PARTITIONED'
    return report


def render_markdown(report):
    lines = ['# RSI 自身只读运行报告', '', '数据指纹：`' + report['source_fingerprint'] + '`。', '',
             '统计由确定性代码计算；不调用模型，训练采纳与独立验证分别统计。', '',
             '## 能力矩阵', '', '| 模块 | 状态 | 实际入口 | 数据缺口 |', '|---|---|---|---|']
    for row in report['capabilities']:
        lines.append('| ' + ' | '.join(str(row[k]).replace('|', '/') for k in ('module', 'status', 'entrypoint', 'gap')) + ' |')
    lines += ['', '## 分区统计', '']
    for group in report['statistics']['groups']:
        if group.get('metric_contract_version'):
            lines += ['指标契约：`' + group['metric_contract_version'] + '`。', '',
                      'Memory 分母：M1 为唯一 retrieval_id 操作（包含空结果与已启动中断）；M2 为 retrieval_id + entry_id；M3 为 round_id + entry_id。', '']
        lines += ['维度：`' + json.dumps(group['dimensions'], ensure_ascii=False, sort_keys=True) + '`', '',
                  '| 指标 | 状态 | 分子 | 分母 | 值 | 单位 |', '|---|---|---:|---:|---:|---|']
        for m in group['metrics']:
            show = lambda x: 'UNKNOWN' if x is None else str(x)
            lines.append('| ' + ' | '.join([m['metric_id'], m['status'], show(m['numerator']), show(m['denominator']), show(m['value']), m['unit']]) + ' |')
        lines += ['', '六角色/其他已记录角色的调用指标：', '', '```json', json.dumps(group.get('agent_roles', []), ensure_ascii=False, sort_keys=True, indent=2), '```', '',
                  '费用分布（usage、供应商账单、账本上界与未知分别保留）：', '', '```json', json.dumps(group['cost_breakdown'], ensure_ascii=False, sort_keys=True, indent=2), '```', '',
                  'Proposal Portfolio：', '', '```json', json.dumps(group['portfolio'], ensure_ascii=False, sort_keys=True, indent=2), '```', '']
    if not report['statistics']['groups']:
        lines += ['无符合筛选条件的可观测记录；指标 N/A/UNKNOWN，不填零。', '']
    current = any(g.get('metric_contract_version') for g in report['statistics']['groups'])
    lines += ['## 搜索与回放、Memory、独立核查', '',
              '父代分布、完整家族回放、训练裁决、Memory 和 Holdout 指标见各分区 S/P/M 系列；缺记录即保留未知。', '',
              '## 错误与缺口', '', '传输状态：`' + json.dumps(report['transport_status_distribution'], sort_keys=True) + '`。', '',
              *['- ' + gap for gap in report['evidence_gaps']], '', '## M1 设计入口', '',
              report['next_stage']['recommendation'], '',
              '当前包含 M1-A.1 运行观测；版本分区独立统计，未实施后续能力。' if current else 'M0 完成后停止；未实施 M1。', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-log', action='append', default=[])
    parser.add_argument('--registry', action='append', default=[])
    parser.add_argument('--budget-ledger', action='append', default=[])
    parser.add_argument('--source-manifest', help='Explicit paths and expected SHA256 values; never discovers closed files.')
    parser.add_argument('--artifact-manifest', help='Explicit {kind,path,sha256} entries; public metadata only.')
    parser.add_argument('--output', type=Path, required=True)
    for field in ('experiment_id', 'epoch', 'role_profile', 'agent_role', 'evidence_mode', 'parent_id', 'candidate_id', 'compatibility_fingerprint'):
        parser.add_argument('--' + field.replace('_', '-'))
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError('ANALYTICS_NEW_OUTPUT_DIRECTORY_REQUIRED')
    paths = list(args.event_log)
    paths.extend(args.budget_ledger)
    for registry in args.registry:
        root = Path(registry).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('ANALYTICS_REGISTRY_DIRECTORY_REQUIRED')
        found = [root / name for name in ('events.sqlite', 'policy.sqlite', 'lineage.sqlite', 'parents.sqlite', 'learning-links.sqlite') if (root / name).is_file()]
        if not found:
            raise ValueError('ANALYTICS_REGISTRY_HAS_NO_DIRECT_EVENT_LOGS')
        paths.extend(map(str, found))
    expected = {}
    if args.source_manifest:
        entries = json.loads(Path(args.source_manifest).read_text(encoding='utf-8-sig'))['sources']
        for entry in entries:
            path = str(Path(entry['path']).resolve(strict=True))
            if path in expected and expected[path] != entry['sha256']:
                raise ValueError('ANALYTICS_MANIFEST_CONFLICT')
            expected[path] = entry['sha256']
            paths.append(path)
    if not paths:
        raise ValueError('ANALYTICS_EXPLICIT_SOURCES_REQUIRED')
    artifacts = json.loads(Path(args.artifact_manifest).read_text(encoding='utf-8-sig'))['artifacts'] if args.artifact_manifest else []
    filters = {name: getattr(args, name) for name in DIMENSIONS if hasattr(args, name) and getattr(args, name) is not None}
    report = build_report(sorted(set(paths)), artifacts, filters, expected)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'report.json').write_bytes(canonical(report))
    (args.output / 'report.zh.md').write_text(render_markdown(report), encoding='utf-8')
    contracts = {g['metric_contract_version']: g['metric_definitions'] for g in report['statistics']['groups'] if g.get('metric_contract_version')}
    schema = {'legacy': METRICS, 'contracts': contracts} if contracts else METRICS
    (args.output / 'metrics-schema.json').write_bytes(canonical(schema))
    print(json.dumps({'output': str(args.output), 'source_fingerprint': report['source_fingerprint'],
                      'projection_count': report['projection_count'], 'model_calls': 0, 'source_writes': 0}))


if __name__ == '__main__':
    main()
