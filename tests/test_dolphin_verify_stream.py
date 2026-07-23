import asyncio
import os
from app.services.dolphin_tool import DolphinToolService


def _pid_exists(pid: int) -> bool:
    """Return True while the OS still knows about ``pid``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_verify_stream_cleans_up_subprocess_when_cancelled(tmp_path):
    """Cancelling a dolphin verify stream must terminate its child process."""
    asyncio.run(_verify_stream_cleans_up_subprocess_when_cancelled(tmp_path))


async def _verify_stream_cleans_up_subprocess_when_cancelled(tmp_path):
    """Spawn a long-running fake dolphin-tool, cancel mid-stream, assert cleanup."""
    fake_dolphin = tmp_path / "fake_dolphin.py"
    fake_dolphin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "import time\n"
        "print('Verifying: 1%')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    fake_dolphin.chmod(0o755)

    service = DolphinToolService()
    service.dolphin_tool_path = str(fake_dolphin)
    updates = []

    async def consume_updates():
        async for update in service.verify_stream(str(tmp_path / "sample.rvz")):
            updates.append(update)

    task = asyncio.create_task(consume_updates())
    for _ in range(50):
        if updates and service.active_pids():
            break
        await asyncio.sleep(0.05)

    assert updates and service.active_pids(), (
        "verify_stream did not start streaming in time"
    )
    assert updates[0]["type"] == "progress"
    pid = service.active_pids()[0]
    await asyncio.sleep(0.1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("verify_stream consumer was not cancelled")

    assert service.active_pids() == []
    assert not _pid_exists(pid)
