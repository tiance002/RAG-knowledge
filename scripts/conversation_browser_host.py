"""Serve the isolated acceptance API and built UI on one loopback origin."""
from importlib import import_module
import os
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = import_module(os.getenv('CONVERSATION_BROWSER_APP', 'scripts.conversation_acceptance_app')).app
dist = Path(__file__).resolve().parents[1] / 'frontend-weknora' / 'dist'
app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='acceptance-assets')


@app.get('/{path:path}', include_in_schema=False)
def frontend(path: str):
    return FileResponse(dist / 'index.html')
