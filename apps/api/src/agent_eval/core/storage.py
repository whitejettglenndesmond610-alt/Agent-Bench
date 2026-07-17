import os

from agent_eval.core.settings import Settings


def ensure_storage(settings: Settings) -> None:
    os.umask(0o077)
    for path in (settings.data_dir, settings.backup_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    for path in settings.data_dir.iterdir():
        if path.is_file():
            path.chmod(0o600)
