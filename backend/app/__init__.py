"""
应用包初始化
"""

__all__ = ["app"]


def __getattr__(name):
    if name == 'app':
        from app.main import app
        return app
    raise AttributeError(name)
