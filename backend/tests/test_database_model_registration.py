"""The database engine registers models before callers create metadata."""
import subprocess
import sys
from pathlib import Path
import pytest


@pytest.mark.parametrize("first_module", ["app.core.database", "app.services.runtime_settings", "app.models"])
def test_create_all_in_fresh_process_registers_system_settings(first_module):
    backend = Path(__file__).resolve().parents[1]
    code = """
from sqlmodel import SQLModel, create_engine
from sqlalchemy import inspect
from app.core.database import engine
isolated = create_engine('sqlite://')
SQLModel.metadata.create_all(isolated)
assert inspect(isolated).has_table('system_settings')
isolated.dispose()
engine.dispose()
"""
    result = subprocess.run([sys.executable, "-c", f"import {first_module}\n" + code], cwd=backend,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
