import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.trace_schema import capture_payload, capture_scope, authorized_inline, read_snapshot, save_snapshot


class CapturePolicyTests(unittest.TestCase):
    def test_default_and_incomplete_manifest_fail_closed(self):
        manifest = {'entries': [{'dataset_version': 'v1', 'qid': 'q1', 'source_sha256': 'a'*64, 'cloud_allowed': True}]}
        with capture_scope(qid='q1', dataset_version='v1', capture_mode='EVAL',
                           source_sha256s=['a'*64, 'b'*64], manifest=manifest):
            payload = capture_payload('private document', content_role='provider_request')
        self.assertEqual(payload['capture_mode'], 'BASIC')
        self.assertNotIn('text', payload)
        self.assertEqual(payload['content_visibility'], 'NOT_CAPTURED')

    def test_authorized_envelope_survives_scope_exit_and_denies_tampering(self):
        manifest = {'entries': [{'dataset_version': 'v1', 'qid': 'q1', 'source_sha256': 'a'*64, 'cloud_allowed': True}]}
        with capture_scope(qid='q1', dataset_version='v1', capture_mode='EVAL',
                           source_sha256s=['a'*64], manifest=manifest):
            payload = capture_payload('public evidence', content_role='provider_request')
        self.assertTrue(authorized_inline(payload))
        self.assertFalse(authorized_inline({**payload, 'text': 'private evidence'}))
        self.assertFalse(authorized_inline({**payload, 'qid': 'other'}))
        self.assertFalse(authorized_inline({**payload, '_capture_authorization': 'forged'}))

    def test_secrets_never_enter_local_storage(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ('Authorization: Bearer do-not-export', 'postgresql://user:credential@host/db',
                          '<think>private reasoning</think>', '<thinking>private reasoning</thinking>',
                          'Thinking: private reasoning', {'reasoning_content': 'private reasoning'},
                          {'role': 'assistant', 'content': 'visible\nThinking: private reasoning'}):
                p = capture_payload(value, content_role='provider_request', root=root, save_local=True)
                self.assertEqual(p['content_visibility'], 'REDACTED')
            self.assertEqual(list(Path(root).iterdir()), [])

    @unittest.skipUnless(os.name == 'posix', 'POSIX private storage; Windows ACL not assumed')
    def test_snapshot_corruption_permissions_path_and_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            ref = save_snapshot(b'actual immutable bytes', root=root)
            self.assertEqual(read_snapshot(ref, root=root), b'actual immutable bytes')
            self.assertEqual(save_snapshot(b'actual immutable bytes', root=root), ref)
            with self.assertRaises(ValueError): read_snapshot('../outside', root=root)
            with self.assertRaises(ValueError): save_snapshot(b'oversize', root=root, max_bytes=2)
            (Path(root)/(ref+'.txt')).write_bytes(b'corrupted')
            with self.assertRaises(ValueError): read_snapshot(ref, root=root)
            with self.assertRaises(ValueError): save_snapshot(b'actual immutable bytes', root=root)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private storage; Windows ACL not assumed')
    def test_anchored_directory_survives_swap_without_following_replacement(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, moved, attacker = base / 'root', base / 'moved', base / 'attacker'
            root.mkdir(mode=0o700)
            attacker.mkdir(mode=0o700)
            real_open = os.open
            name = hashlib.sha256(b'evidence').hexdigest() + '.txt'
            swapped = []
            def switching_open(path, flags, *args, **kwargs):
                if path == name and not swapped:
                    root.rename(moved)
                    root.symlink_to(attacker, target_is_directory=True)
                    swapped.append(True)
                return real_open(path, flags, *args, **kwargs)
            with patch('app.services.trace_schema.os.open', side_effect=switching_open):
                ref = save_snapshot(b'evidence', root=root)
            self.assertTrue(swapped)
            self.assertEqual((moved/(ref+'.txt')).read_bytes(), b'evidence')
            self.assertFalse((attacker/(ref+'.txt')).exists())
            with self.assertRaises(OSError): read_snapshot(ref, root=root)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private storage; Windows ACL not assumed')
    def test_path_permissions_expiry_size_capacity_and_symlinks(self):
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'root'
            root.mkdir(mode=0o700)
            ref = save_snapshot(b'abcd', root=root, capacity_bytes=5)
            with self.assertRaises(ValueError): save_snapshot(b'new', root=root, capacity_bytes=5)
            with self.assertRaises(ValueError): read_snapshot(ref, root=root, max_bytes=2)
            target = root/(ref+'.txt')
            target.chmod(0o640)
            with self.assertRaises(PermissionError): read_snapshot(ref, root=root)
            target.chmod(0o600)
            old = time.time() - 8 * 86400
            os.utime(target, (old, old))
            with self.assertRaises(ValueError): read_snapshot(ref, root=root)
            target.unlink()
            other = Path(temp)/'other'
            other.write_bytes(b'abcd')
            target.symlink_to(other)
            with self.assertRaises(OSError): read_snapshot(ref, root=root)
            target.unlink()
            root.chmod(0o750)
            with self.assertRaises(PermissionError): save_snapshot(b'abc', root=root)
            link = Path(temp)/'link'
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError): save_snapshot(b'abc', root=link/'child')

    @unittest.skipUnless(os.name == 'posix', 'POSIX private storage; Windows ACL not assumed')
    def test_capacity_lock_serializes_concurrent_writers(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as temp:
            os.chmod(temp, 0o700)
            def write(value):
                try:
                    return save_snapshot(value, root=temp, capacity_bytes=5)
                except ValueError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                refs = list(pool.map(write, (b'aaaa', b'bbbb')))
            self.assertEqual(sum(ref is not None for ref in refs), 1)
            self.assertEqual(sum(p.stat().st_size for p in Path(temp).iterdir()), 4)


if __name__ == '__main__':
    unittest.main()
