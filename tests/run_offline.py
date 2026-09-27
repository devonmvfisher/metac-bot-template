"""Serial unittest runner; deny sockets and subprocesses throughout discovery/run."""
from pathlib import Path
import socket
import subprocess
import asyncio
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError("OFFLINE TEST attempted external access")


def main():
    def audit(event, args):
        if event in ("subprocess.Popen", "os.system"):
            denied()
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
                denied()
    # Windows asyncio uses loopback socket pairs for its internal wake-up pipe.
    # No external connection, subprocess, or live client is permitted.
    sys.addaudithook(audit)
    socket.create_connection = denied
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv) > 1 else unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
