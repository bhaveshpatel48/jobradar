"""Test isolation: every test module does `import tests` FIRST.

`python -m unittest discover tests` imports test files as top-level modules, so
this package's __init__ would not otherwise run. Because `python -m` puts the
project root on sys.path, an explicit `import tests` always resolves here, in
any discovery mode.

config.py reads DB_PATH once per process, and it also loads .env (whose
DB_PATH is the live database). So DB_PATH must be forced to a temp file before
any app module is imported, and the result is checked by assert_isolated().
"""

import os
import tempfile

TEST_DIR = os.environ.get("JOBRADAR_TEST_DIR") or tempfile.mkdtemp(prefix="jobradar-tests-")
os.environ["JOBRADAR_TEST_DIR"] = TEST_DIR  # inherited by subprocess-based tests
os.environ["DB_PATH"] = os.path.join(TEST_DIR, "test.db")
os.environ["GEMINI_API_KEY"] = "test-key"  # never let tests reach the real API


def assert_isolated() -> None:
    """Abort the run unless the app is pointed at the temp test database."""
    import config

    db = os.path.realpath(config.DB_PATH)
    if not db.startswith(os.path.realpath(TEST_DIR) + os.sep):
        raise RuntimeError(
            f"refusing to run tests against {config.DB_PATH!r}: not the temp test database. "
            "Each test module must `import tests` before importing app modules."
        )
