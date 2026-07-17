import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize(
    "script",
    [
        "deploy/backup-production.sh",
        "deploy/monitor-production.sh",
        "deploy/systemd/install-backup-timer.sh",
        "deploy/systemd/install-monitor-timer.sh",
    ],
)
def test_operational_shell_scripts_have_valid_syntax(script: str) -> None:
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_monitoring_systemd_units_verify() -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is unavailable")
    subprocess.run(
        [
            analyzer,
            "verify",
            str(ROOT / "deploy/systemd/agent-eval-monitor.service"),
            str(ROOT / "deploy/systemd/agent-eval-monitor.timer"),
        ],
        check=True,
    )
