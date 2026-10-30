"""SYNTHETIC temporary Git/worktree/DGM inheritance probe; never opens RAG data."""
import argparse
import ast
import hashlib
import json
import math
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path


def load_functions(path, names, namespace):
    tree = ast.parse(Path(path).read_text(encoding='utf8'))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dgm-source', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    commands = []
    started = time.perf_counter()

    def git(cwd, *argv, check=True):
        result = subprocess.run(['git', '-c', 'core.autocrlf=false', *argv], cwd=cwd, capture_output=True)
        commands.append({'argv': ['git', *argv], 'exit_code': result.returncode, 'stdout': result.stdout.decode('utf8', 'replace'), 'stderr': result.stderr.decode('utf8', 'replace')})
        if check and result.returncode:
            raise RuntimeError(commands[-1])
        return result

    namespace = {'os': os, 'json': json, 'math': math, 'random': random, 'load_json_file': lambda p: json.loads(Path(p).read_text())}
    upstream = Path(args.dgm_source)
    load_functions(upstream / 'utils/evo_utils.py', ['get_model_patch_paths'], namespace)
    load_functions(upstream / 'DGM_outer.py', ['choose_selfimproves', 'update_archive'], namespace)
    with tempfile.TemporaryDirectory(prefix='rag-rsi-git-synthetic-') as temp:
        scratch = Path(temp)
        repo = scratch / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.name', 'Synthetic')
        git(repo, 'config', 'user.email', 'synthetic@example.invalid')
        (repo / 'candidate.txt').write_bytes(b'policy=base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'synthetic base')
        base = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        edit, restore = scratch / 'edit', scratch / 'restore'
        git(repo, 'worktree', 'add', '--detach', str(edit), base)
        ancestry = []
        parent = base
        for generation in range(1, 4):
            contents = b'policy=base\n' + b''.join(f'step{i}=synthetic\n'.encode() for i in range(1, generation + 1))
            (edit / 'candidate.txt').write_bytes(contents)
            git(edit, 'add', '.')
            git(edit, 'commit', '-qm', f'synthetic generation {generation}')
            child = git(edit, 'rev-parse', 'HEAD').stdout.decode().strip()
            delta = git(edit, 'diff', '--binary', parent, child).stdout
            archive_dir = out / f'c{generation}'
            archive_dir.mkdir()
            (archive_dir / 'model_patch.diff').write_bytes(delta)
            metadata = {'candidate_id': f'c{generation}', 'parent_commit': 'initial' if generation == 1 else f'c{generation-1}', 'git_commit': child, 'parent_git_commit': parent, 'patch_sha256': hashlib.sha256(delta).hexdigest()}
            (archive_dir / 'metadata.json').write_text(json.dumps(metadata, indent=2))
            ancestry.append(metadata)
            parent = child
        ordered = namespace['get_model_patch_paths'](str(out), '', 'c3')
        assert [Path(p).parent.name for p in ordered] == ['c1', 'c2', 'c3']
        git(repo, 'worktree', 'add', '--detach', str(restore), base)
        wrong_order = git(restore, 'apply', '--check', ordered[-1], check=False)
        assert wrong_order.returncode != 0
        for path in ordered:
            git(restore, 'apply', '--check', path)
            git(restore, 'apply', '--index', path)
        final_tree = git(edit, 'write-tree').stdout.decode().strip()
        restored_tree = git(restore, 'write-tree').stdout.decode().strip()
        assert final_tree == restored_tree
        digest = hashlib.sha256((restore / 'candidate.txt').read_bytes()).hexdigest()
        # Official helper warns and skips a missing patch. Verify and preserve this finding.
        missing = out / 'c2/model_patch.diff'
        data = missing.read_bytes()
        missing.unlink()
        incomplete = namespace['get_model_patch_paths'](str(out), '', 'c3')
        missing.write_bytes(data)
        assert len(incomplete) == 2
        for name, score, parent_id in [('initial', .1, None), ('low', .2, 'initial'), ('high', .9, 'initial')]:
            d = scratch / 'selection' / name
            d.mkdir(parents=True)
            record = {'parent_commit': parent_id, 'overall_performance': {'accuracy_score': score, 'total_resolved_ids': ['s1'], 'total_unresolved_ids': ['s2'], 'total_emptypatch_ids': []}}
            (d / 'metadata.json').write_text(json.dumps(record))
        random.seed(5)
        selection = namespace['choose_selfimproves'](str(scratch / 'selection'), ['initial', 'low', 'high'], 1, method='best', polyglot=True)
        assert selection[0][0] == 'initial'  # Pinned source sorts ascending.
        archive = namespace['update_archive']('', ['initial'], ['low', 'high'])
        assert archive == ['initial', 'low', 'high']
        # Explicit cleanup supports replay evidence without preserving temporary worktrees.
        git(repo, 'worktree', 'remove', '--force', str(restore))
        git(repo, 'worktree', 'remove', '--force', str(edit))
        listing = git(repo, 'worktree', 'list', '--porcelain').stdout.decode()
        assert listing.count('worktree ') == 1
        scratch_path = str(scratch)
    summary = {'status': 'PASS', 'kind': 'SYNTHETIC', 'generations': 3, 'parent_relative_patch_order': [Path(p).parent.name for p in ordered], 'tree_hash': restored_tree, 'candidate_sha256': digest, 'wrong_order_rejected_exit': wrong_order.returncode, 'official_missing_patch_behavior': 'WARN_AND_SKIP', 'official_best_selection': selection[0][0], 'official_keep_all': archive, 'scratch_removed': not Path(scratch_path).exists(), 'duration_seconds': time.perf_counter() - started, 'external_model_calls': 0}
    (out / 'commands.json').write_text(json.dumps(commands, indent=2))
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
