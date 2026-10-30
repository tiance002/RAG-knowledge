"""SYNTHETIC bounded workflow search using pinned AFlow utility class bodies.

No Optimizer/model stack import; AST extraction executes unchanged class bodies.
"""
import argparse
import ast
import datetime
import json
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    logs = []

    class Logger:
        def info(self, s): logs.append(str(s))
        warning = error = info

    def write(path, data, **kw):
        Path(path).write_text(json.dumps(data, indent=4, default=str), encoding='utf8')

    env = dict(globals(), logger=Logger(), read_json_file=lambda path, **kw: json.loads(Path(path).read_text(encoding='utf8')), write_json_file=write)
    for name, file in [('DataUtils', 'data_utils.py'), ('ExperienceUtils', 'experience_utils.py')]:
        path = Path(args.source) / 'scripts/optimizer_utils' / file
        tree = ast.parse(path.read_text(encoding='utf8'))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
    data = env['DataUtils'](str(root))
    experience = env['ExperienceUtils'](str(root))
    started = time.perf_counter()
    np.random.seed(9)
    random.seed(9)
    workflows = root / 'workflows'
    workflows.mkdir()

    def evaluate(operators):
        traces, scores = [], []
        for case in ['simple', 'table']:
            evidence = {'field_a'}
            events = [{'operator': 'retrieve', 'evidence': sorted(evidence)}]
            for op in operators[1:]:
                if op == 'expand': evidence.add('field_b')
                if op == 'drop_source': evidence.clear()
                events.append({'operator': op, 'evidence': sorted(evidence)})
            needed = {'field_a'} if case == 'simple' else {'field_a', 'field_b'}
            scores.append(float(needed <= evidence))
            traces.append({'synthetic_case': case, 'events': events, 'score': scores[-1]})
        return sum(scores) / len(scores), traces

    baseline = ['retrieve', 'generate']
    score, trace = evaluate(baseline)
    nodes = [{'round': 1, 'parent': None, 'operators': baseline, 'score': score, 'traces': trace}]
    for child_id, ops in [(2, ['retrieve', 'expand', 'generate']), (3, ['retrieve', 'drop_source', 'generate'])]:
        # The mutation is scripted, never attributed to an autonomous LLM.
        parent = data.select_round([{'round': 1, 'score': score}])
        after, trace = evaluate(ops)
        directory = workflows / f'round_{child_id}'
        directory.mkdir()
        exp = experience.create_experience_data(parent, 'synthetic ' + ops[1])
        experience.update_experience(str(directory), exp, after)
        nodes.append({'round': child_id, 'parent': parent['round'], 'operators': ops, 'score': after, 'traces': trace})
    processed = experience.load_experience()
    formatted = experience.format_experience(processed, 1)
    assert 'Absolutely prohibit synthetic expand' in formatted
    assert processed[1]['success'][2]['score'] == 1.0
    assert processed[1]['failure'][3]['score'] == 0.0
    # Check actual selection preference and exploration on a multi-candidate pool.
    draws = [data.select_round([{'round': n['round'], 'score': n['score']} for n in nodes])['round'] for _ in range(20)]
    probabilities = data._compute_probabilities([100, 50, 0]).tolist()
    assert abs(sum(probabilities) - 1) < 1e-9 and all(p > 0 for p in probabilities)
    # Source only retains seed when the seed is among top sample rounds.
    write(workflows / 'results.json', [{'round': n['round'], 'score': n['score']} for n in nodes])
    top = data.get_top_rounds(1)
    assert top[0]['round'] == 2
    result = {'status': 'PASS', 'kind': 'SYNTHETIC', 'utility_source_execution': 'UNCHANGED_AST_CLASS_BODIES', 'full_optimizer': 'NOT_RUN', 'nodes': nodes, 'top_one': top, 'mixed_probabilities': probabilities, 'selection_draws': draws, 'success_formatted_as_prohibition': True, 'external_model_calls': 0, 'duration_seconds': time.perf_counter() - started}
    (root / 'summary.json').write_text(json.dumps(result, indent=2))
    (root / 'experience-prompt.txt').write_text(formatted)
    (root / 'utility.log').write_text('\n'.join(logs))
    print(json.dumps({k:v for k,v in result.items() if k != 'nodes'}))


if __name__ == '__main__':
    main()
