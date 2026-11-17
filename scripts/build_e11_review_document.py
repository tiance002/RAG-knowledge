"""Build the single E1 adjudication handoff from immutable local evidence."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / 'var' / 'e1-20261029'


def load_jsonl(name):
    return [json.loads(line) for line in (DIR / name).read_text(encoding='utf-8').splitlines() if line.strip()]


def keyed(rows):
    result = {r['qid']: r for r in rows}
    if len(result) != len(rows):
        raise ValueError('duplicate qid')
    return result


def events(path):
    answer, citations, terminal = [], [], []
    for block in path.read_text(encoding='utf-8').replace('\r\n', '\n').split('\n\n'):
        parts = block.splitlines()
        if not parts or not parts[0].startswith('event: '):
            continue
        data = next((line[6:] for line in parts if line.startswith('data: ')), None)
        if data is None:
            continue
        kind, value = parts[0][7:], json.loads(data)
        if kind == 'answer_chunk': answer.append(value.get('content', ''))
        elif kind == 'rag_result': citations = value.get('citations', [])
        elif kind in ('done', 'error'): terminal.append(kind)
    return ''.join(answer), citations, terminal


def main():
    ledger, gold = keyed(load_jsonl('ledger-v4.jsonl')), keyed(load_jsonl('e1_candidates.jsonl'))
    audit24, audit6 = keyed(load_jsonl('review24-independent-20261029.jsonl')), keyed(load_jsonl('review6-independent-20261029.jsonl'))
    if len(ledger) != 30 or set(ledger) != set(gold) or set(ledger) != (set(audit24) | set(audit6)) or set(audit24) & set(audit6):
        raise ValueError('30-qid ledger/gold/audit mismatch')
    output = []
    for qid, row in ledger.items():
        path = ROOT / row['sse']
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sse_sha256']:
            raise ValueError('SSE hash mismatch: ' + qid)
        answer, cites, terminal = events(path)
        if terminal != ['done'] or not answer:
            raise ValueError('incomplete answer: ' + qid)
        review = audit24.get(qid) or audit6[qid]
        markers = sorted({int(n) for n in re.findall(r'\[(\d+)\]', answer)})
        relevant = []
        for n in markers:
            if 1 <= n <= len(cites):
                c = cites[n-1]
                relevant.append({'citation_id': n, 'chunk_id': c.get('chunk_id'),
                                 'source_path_or_name': c.get('fileName') or c.get('file_name'),
                                 'position': c.get('location'), 'source_excerpt': c.get('content')})
        ai_core = review.get('core_judgment')
        output.append({'qid': qid, 'question': gold[qid].get('question'), 'answer': answer,
                       'gold': gold[qid].get('gold'),
                       'core_verdict': row.get('human_core') if qid in audit24 else ai_core,
                       'reviewer_type': 'USER' if qid in audit24 else 'AI_DELEGATED',
                       'rubric': row.get('gold_rubric'),
                       'sse_path': str(path), 'sse_sha256': row['sse_sha256'],
                       'answer_sha256': hashlib.sha256(answer.encode('utf-8')).hexdigest(),
                       'trace_ids': row.get('result_case', {}).get('trace_ids', []),
                       'source_audit': gold[qid].get('evidence'),
                       'actual_answer_markers': markers,
                       'unresolved_citation_markers': [n for n in markers if n < 1 or n > len(cites)],
                       'actual_sse_citations': relevant,
                       'source_findings': review.get('independent_findings', []),
                       'source_claims': review.get('claims', []),
                       'previous_audit_evidence': review.get('prior_audit_evidence', []),
                       'source_review_file': str(DIR / ('review24-independent-20261029.jsonl' if qid in audit24 else 'review6-independent-20261029.jsonl')),
                       'user_decision_required': False,
                       'decision_request': None})
    data = {'scope': 'E1 30题既有复核汇总；来源不足保留 UNKNOWN，不把 AI 审核标为用户判定。',
            'pending_decisions': [],
            'limitations': ['既有24题原文核查多数沿用前次审查，并非全部第二次独立核验。',
                            '引用片段来自该次 SSE，来源陈述不等同真实性、临床安全或 UI 可点击。',
                            '部分历史中文捕获存在编码损坏，保留原始字节语义，不修复或猜测。',
                            '层数、时间代理、Chandamama Gold、服务器历史口径遵循已有用户决定，不重新提问。'],
            'records': output}
    json_path = DIR / 'E1-统一待裁决问题.json'
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    md = '''# E1 统一待裁决问题

**目前没有需要项目负责人裁决的新增评分口径问题。** 已复用 30 题逐题复核、原始 SSE 和冻结 Gold：前 24 题保留用户核心判断 23 PASS / 1 FAIL；后 6 题保留用户授权的 AI 核心判断 4 PASS / 2 FAIL，不能合称全部人工评分。原始评审与 Gold 均未修改。

层数按包含输入层共 5 层；完成时间题使用用户接受的出版/上映日期代理；Chandamama 使用冻结 Telugu/Tamil Gold；服务器题按历史资料中的用途和质量差异并保留相反观点。这四项已有口径，不再提问。出版/上映日期不能证明实际创作完成时间。

以下是**证据或技术限制，不需要用户凭猜测投票**：

- Office 008 的成本格为空，回答写成 `na`；Office 020 的修订备注属于完成率格，回答归给预算格。逐项证据与 SSE 引用保存在配套 JSON；这些问题与用户的核心答案判断分开。
- 党员活动题 [6] 不支持相邻党章学习陈述，[13] 有相关内容；伊思题 [5] 是其他品牌，白色蜗牛霜与系列名称是否同一仍为 UNKNOWN；导演角色与姓名关联在既有材料范围内未充分确证，仍为 UNKNOWN。
- 转氨酶题“五倍以下问题不大”可在当次来源 [1] 找到，但来源记载不构成可靠临床判断。游戏和服务器材料含历史观点，不能当作当前事实保证。
- 前端引用点击未验证；原始记录中的部分中文出现编码损坏，不能仅凭上下文修复；24 题的多数原文检查沿用既有审计，不能称为全部重新独立核验。

[查看 30 题结构化证据](E1-统一待裁决问题.json)：每题包含问题、原始 SSE 答案、冻结 Gold、评判者类别、已记录的判定、SSE/答案哈希、引用编号及对应实际片段、来源审计和局限。未判定的证据不写成错误或正确；该文件是汇总，不覆盖原始账本和审核材料。
'''
    (DIR / 'E1-统一待裁决问题.md').write_text(md, encoding='utf-8')
    print(json.dumps({'rows': len(output), 'user_review': len(audit24), 'ai_review': len(audit6), 'sse_verified': len(output), 'pending_decisions': 0, 'json': str(json_path)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
