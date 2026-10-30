"""SYNTHETIC Linux probe: scratch text only; no RAG data, keys, Gold or models."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    output = Path(args.output).resolve()
    assert output.parent == Path('/var/log/rag-rsi-preparation')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    image = 'sha256:fbdbaea47b9ae4ecc2082ecdb4e1cea81e32176ffb1dcf643d422ad07427e5d9'
    commands, specs, containers = [], [], []
    prefix = 'rsi-prep-' + uuid.uuid4().hex[:12]
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    scratch = Path(tempfile.mkdtemp(prefix='rag-rsi-preparation-', dir='/var/tmp'))
    started = time.perf_counter()
    disk_before = shutil.disk_usage('/')

    def run(argv, *, check=True, cwd=None):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env, cwd=cwd)
        commands.append({'argv': argv, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if check and result.returncode:
            raise RuntimeError(json.dumps(commands[-1]))
        return result

    before = set(run(['docker', 'ps', '-q', '--no-trunc']).stdout.split())

    def container(stage, mounts, script):
        argv = ['docker', 'create', '--pull=never', '--name', prefix + '-' + stage, '--label', 'rsi-preparation.synthetic=true', '--label', 'rsi-preparation.owner=' + prefix, '--network=none', '--read-only', '--memory=128m', '--memory-swap=128m', '--cpus=0.5', '--pids-limit=32', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534', '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=8m,uid=65534,gid=65534']
        for source, dest, read_only in mounts:
            assert source.resolve().is_relative_to(scratch)
            argv += ['--mount', f'type=bind,src={source},dst={dest}' + (',readonly' if read_only else '')]
        argv += ['--entrypoint=/bin/sh', image, '-c', script]
        ident = run(argv).stdout.strip()
        containers.append(ident)
        config = json.loads(run(['docker', 'inspect', '--format', '{{json .HostConfig}}', ident]).stdout)
        assert config['Memory'] == config['MemorySwap'] == 134217728
        assert config['NanoCpus'] == 500000000 and config['PidsLimit'] == 32
        assert config['NetworkMode'] == 'none' and config['ReadonlyRootfs'] is True
        specs.append({'stage': stage, 'id': ident, 'image_id': image, 'memory': config['Memory'], 'memory_swap': config['MemorySwap'], 'nano_cpus': config['NanoCpus'], 'pids_limit': config['PidsLimit'], 'network_mode': config['NetworkMode'], 'readonly_rootfs': config['ReadonlyRootfs'], 'mounts': [{'source':str(p),'destination':d,'readonly':ro} for p,d,ro in mounts]})
        run(['docker', 'start', '-a', ident])
        state = json.loads(run(['docker', 'inspect', '--format', '{{json .State}}', ident]).stdout)
        assert state['ExitCode'] == 0 and not state['OOMKilled']
        run(['docker', 'rm', ident])
        return ident

    status = 'FAIL'
    try:
        for name in ['input', 'modify-out', 'evaluate-out', 'candidate']:
            p = scratch / name
            p.mkdir(mode=0o755)
            if name.endswith('-out'):
                os.chown(p, 65534, 65534)
                p.chmod(0o700)
        baseline = scratch / 'input/candidate.txt'
        baseline.write_text('policy=base\n')
        baseline.chmod(0o444)
        baseline_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()
        limits = '''set -eu
printf 'memory_max='; cat /sys/fs/cgroup/memory.max
printf 'cpu_max='; cat /sys/fs/cgroup/cpu.max
printf 'pids_max='; cat /sys/fs/cgroup/pids.max
test ! -e /var/run/docker.sock
'''
        modifier = limits + '''if (printf forbidden > /input/candidate.txt) 2>/tmp/readonly-error; then exit 81; fi
printf 'readonly_input=PASS\n'
sed 's/policy=base/policy=child/' /input/candidate.txt > /tmp/candidate.txt
set +e
diff -u --label a/candidate.txt --label b/candidate.txt /input/candidate.txt /tmp/candidate.txt > /output/model_patch.diff
diff_exit=$?
set -e
test "$diff_exit" -eq 1
sha256sum /tmp/candidate.txt > /output/candidate.sha256
printf 'memory_peak='; cat /sys/fs/cgroup/memory.peak
'''
        mod_id = container('modify', [(scratch/'input', '/input', True), (scratch/'modify-out', '/output', False)], modifier)
        patch = scratch / 'modify-out/model_patch.diff'
        assert patch.stat().st_size > 0
        candidate = scratch / 'candidate'
        (candidate / 'candidate.txt').write_text(baseline.read_text())
        run(['git', 'init', '-q'], cwd=candidate)
        run(['git', 'apply', '--check', str(patch)], cwd=candidate)
        run(['git', 'apply', str(patch)], cwd=candidate)
        candidate_hash = hashlib.sha256((candidate / 'candidate.txt').read_bytes()).hexdigest()
        exported_hash = (scratch / 'modify-out/candidate.sha256').read_text().split()[0]
        assert candidate_hash == exported_hash
        evaluator = limits + '''if (printf forbidden > /candidate/candidate.txt) 2>/tmp/readonly-error; then exit 82; fi
test "$(cat /candidate/candidate.txt)" = 'policy=child'
sha256sum /candidate/candidate.txt > /output/verified.sha256
printf 'readonly_candidate=PASS\n'
printf 'memory_peak='; cat /sys/fs/cgroup/memory.peak
'''
        eval_id = container('evaluate', [(candidate, '/candidate', True), (scratch/'evaluate-out', '/output', False)], evaluator)
        assert mod_id != eval_id
        assert (scratch / 'evaluate-out/verified.sha256').read_text().split()[0] == candidate_hash
        assert hashlib.sha256(baseline.read_bytes()).hexdigest() == baseline_hash
        shutil.copy2(patch, output / 'model_patch.diff')
        status = 'PASS'
    finally:
        for ident in containers:
            p = run(['docker', 'inspect', '--format', '{{index .Config.Labels "rsi-preparation.owner"}}', ident], check=False)
            if p.returncode == 0:
                assert p.stdout.strip() == prefix
                run(['docker', 'rm', '-f', ident])
        assert scratch.resolve().parent == Path('/var/tmp') and scratch.name.startswith('rag-rsi-preparation-') and not scratch.is_symlink()
        shutil.rmtree(scratch)
        after = set(run(['docker', 'ps', '-q', '--no-trunc']).stdout.split())
        assert before == after
        leftover = run(['docker', 'ps', '-aq', '--filter', 'label=rsi-preparation.owner=' + prefix]).stdout.strip()
        assert not leftover
        disk_after = shutil.disk_usage('/')
        summary = {'status': status, 'kind': 'SYNTHETIC', 'image_id': image, 'containers_created': len(containers), 'containers_left': 0, 'production_running_ids_unchanged': before == after, 'scratch_removed': not scratch.exists(), 'candidate_sha256': locals().get('candidate_hash'), 'external_model_calls': 0, 'duration_seconds': time.perf_counter() - started, 'disk_free_before': disk_before.free, 'disk_free_after': disk_after.free, 'specs': specs}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
        (output / 'commands.json').write_text(json.dumps(commands, indent=2))
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
