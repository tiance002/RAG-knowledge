"""Zero-provider regression for the real runner's SQLite cleanup order."""
import runpy
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlmodel import Session

RUNNER = Path(__file__).resolve().parents[2] / 'scripts' / 'query_retrieval_acceptance.py'
close_session_and_engine = runpy.run_path(str(RUNNER))['close_session_and_engine']


class CleanupOrderTests(unittest.TestCase):
    def check_cleanup(self, fail):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine('sqlite:///' + str(Path(directory) / 'test.db'))
            seen = []
            with Session(engine) as session:
                event.listen(engine, 'engine_disposed', lambda _: seen.append(session.in_transaction()))
                try:
                    session.execute(text('SELECT 1'))
                    self.assertTrue(session.in_transaction())
                    if fail:
                        raise ValueError('original error')
                finally:
                    close_session_and_engine(session, engine)
                    self.assertEqual(seen, [False])

    def test_normal_cleanup(self):
        self.check_cleanup(False)

    def test_exception_cleanup_preserves_original(self):
        with self.assertRaisesRegex(ValueError, '^original error$'):
            self.check_cleanup(True)


if __name__ == '__main__':
    unittest.main()
