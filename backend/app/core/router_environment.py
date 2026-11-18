"""Load credential-free router controls without overriding process environment."""
import os
from pathlib import Path
from dotenv import dotenv_values
from dotenv.parser import parse_stream


ROUTER_ENV_KEYS = frozenset({
    'RAG_MODEL_ROUTER_' + name for name in (
        'MODE', 'CHEAP_MODEL', 'EXPENSIVE_MODEL', 'THRESHOLD', 'SELECTION_POLICY',
        'TASK_RISK_FLOOR', 'UNKNOWN_POLICY', 'ESCALATE_ON_QUALITY_FAIL',
        'ESCALATE_ON_TRUNCATED', 'ESCALATE_ON_PROVIDER_FALLBACK',
        'ESCALATE_ON_UNSUPPORTED', 'OFF_FILE',
        'WEIGHT_T', 'WEIGHT_C', 'WEIGHT_J', 'WEIGHT_D', 'WEIGHT_L', 'WEIGHT_M')
} | {
    'RAG_ROUTER_CHEAP_MODEL_ID', 'RAG_ROUTER_EXPENSIVE_MODEL_ID',
    'RAG_ROUTER_CHEAP_ENABLE_THINKING', 'RAG_ROUTER_EXPENSIVE_ENABLE_THINKING',
    'RAG_ROUTER_ALLOW_MODEL_CALLS', 'RAG_ROUTER_QUALITY_CHECK',
})


def load_router_environment(path=None, *, env=None, project_dir=None):
    """Copy allowlisted values only when absent; reject a file before any write.

    Missing default files preserve existing defaults. Tests may pass an explicit
    mapping and path to avoid reading or changing the real process environment.
    Interpolation is disabled so environment secrets cannot enter router values.
    """
    env = os.environ if env is None else env
    project_dir = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parents[3]
    selected = Path(path or env.get('RAG_MODEL_ROUTER_ENV_FILE') or 'config/model-router.env')
    if not selected.is_absolute():
        selected = project_dir / selected
    if not selected.exists():
        return ()
    with selected.open(encoding='utf-8-sig') as stream:
        if any(binding.error for binding in parse_stream(stream)):
            raise ValueError('router environment file contains malformed entries')
    values = dotenv_values(selected, encoding='utf-8-sig', interpolate=False)
    if any(key not in ROUTER_ENV_KEYS for key in values):
        raise ValueError('router environment file contains non-allowlisted keys')
    if any(value is None for value in values.values()):
        raise ValueError('router environment file requires explicit key=value entries')
    loaded = []
    for key, value in values.items():
        if key not in env:
            env[key] = value
            loaded.append(key)
    return tuple(loaded)
