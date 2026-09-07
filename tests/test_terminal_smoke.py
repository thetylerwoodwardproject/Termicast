import errno
import os
import pty
import select
import subprocess
import sys
import time

import pytest

import cli
import store


@pytest.mark.parametrize('reduced', [False, True])
def test_cli_in_real_terminal(data_dir, reduced):
    store.save({'show': {'title': 'Terminal smoke test'}, 'episodes': []})
    master, slave = pty.openpty()
    env = dict(os.environ, TERM='xterm-256color')
    env.pop('TERMICAST_REDUCED_MOTION', None)
    if reduced:
        env['TERMICAST_REDUCED_MOTION'] = '1'
    process = subprocess.Popen([sys.executable, cli.__file__], stdin=slave,
                               stdout=slave, stderr=slave, env=env)
    os.close(slave)
    output = b''
    sent = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output += chunk
            if b'Choice:' in output and not sent:
                os.write(master, b'8\n')
                sent = True
        assert process.wait(timeout=2) == 0, output.decode(errors='replace')
        assert b'TERMICAST' in output
        assert b'Terminal smoke test' in output
        assert b'PUBLISH & MANAGE' in output
        assert b'Bye.' in output
        assert b'Traceback' not in output
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)
