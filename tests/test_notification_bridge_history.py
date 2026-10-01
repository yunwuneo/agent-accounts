"""Run the native bridge's rules/history with offline JVM fixtures only."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_notification_history(tmp_path):
    javac, java = shutil.which("javac"), shutil.which("java")
    if not javac or not java:
        pytest.skip("JDK required for native notification bridge tests")
    bridge = Path(__file__).resolve().parents[1] / "android" / "notification-bridge"
    source = bridge / "src" / "org" / "echo" / "accounts" / "notifications"
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        [
            javac,
            "-encoding",
            "UTF-8",
            "--release",
            "8",
            "-d",
            str(classes),
            str(source / "HistoryStore.java"),
            str(source / "NotificationRules.java"),
            str(bridge / "tests" / "HistoryStoreTest.java"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        [
            java,
            "-cp",
            str(classes),
            "org.echo.accounts.notifications.HistoryStoreTest",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert b"history scenarios passed" in result.stdout
