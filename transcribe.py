"""
transcribe.py - turn an episode's audio into a VTT transcript.

Two backends, picked by pipeline.json's transcription.mode:
  - 'cloud': OpenAI's hosted Whisper API (https://api.openai.com/v1/audio/transcriptions)
  - 'local': shells out to a local whisper binary (openai-whisper's `whisper`
             CLI, whisper.cpp, faster-whisper-cli...) using a configurable
             command template that must write "<basename>.vtt" into --outdir.
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

import requests

OPENAI_TRANSCRIBE_URL = 'https://api.openai.com/v1/audio/transcriptions'
PYTORCH_CPU_INDEX_URL = 'https://download.pytorch.org/whl/cpu'
WHISPER_VENV_DIRNAME = '.venv-whisper'


class TranscriptionError(Exception):
    pass


def check_ready(config):
    """Raise TranscriptionError early if the configured backend can't run,
    before any audio conversion work is done. Cloud mode is checked for an
    API key; local mode is checked for the command's binary being on PATH.
    """
    mode = config['transcription']['mode']
    if mode == 'cloud':
        cfg = config['transcription']['cloud']
        api_key_env = cfg.get('apiKeyEnv', 'OPENAI_API_KEY')
        if not os.environ.get(api_key_env):
            raise TranscriptionError(f'{api_key_env} is not set in the environment.')
    elif mode == 'local':
        cfg = config['transcription']['local']
        cmd_str = cfg['command'].format(input='', outdir='')
        try:
            tokens = shlex.split(cmd_str)
        except ValueError:
            tokens = []
        executable = tokens[0] if tokens else ''
        if executable and not shutil.which(executable):
            raise TranscriptionError(
                f'Local transcription command "{executable}" was not found on PATH.\n'
                f'  Install it (e.g. `pip install -U openai-whisper` for the openai-whisper CLI, '
                f'or install whisper.cpp/faster-whisper if that\'s what your command template uses), '
                f'or switch to cloud transcription in "Configure AI pipeline" > "Transcription (Whisper)".'
            )

        # `python3 -m <module>` (the default) sidesteps PATH issues with the
        # console-script itself, but can still fail if the module isn't
        # installed for *that* interpreter -- check for that specifically,
        # since it's a much clearer failure than a raw ModuleNotFoundError
        # traceback after conversion has already run.
        if executable.startswith('python') and len(tokens) >= 3 and tokens[1] == '-m':
            module = tokens[2]
            check = subprocess.run([executable, '-c', f'import {module}'],
                                    capture_output=True, text=True)
            if check.returncode != 0:
                interp_path = shutil.which(executable) or executable
                raise TranscriptionError(
                    f'"{module}" is not installed for the Python interpreter this command uses '
                    f'({interp_path}).\n'
                    f'  Install it there with `{executable} -m pip install -U openai-whisper`, or '
                    f'if you installed it for a different Python (a venv, a different python3 '
                    f'version, etc.), point the command template at that interpreter instead via '
                    f'"Configure AI pipeline" > "Transcription (Whisper)".'
                )
    else:
        raise TranscriptionError(f'Unknown transcription mode: {mode}')


def transcribe_to_vtt(audio_path, config, dest_vtt_path):
    """Transcribe audio_path and write the result to dest_vtt_path. Returns dest_vtt_path."""
    mode = config['transcription']['mode']
    if mode == 'cloud':
        vtt_text = _transcribe_cloud(audio_path, config)
        with open(dest_vtt_path, 'w', encoding='utf-8') as f:
            f.write(vtt_text)
        return dest_vtt_path
    elif mode == 'local':
        return _transcribe_local(audio_path, config, dest_vtt_path)
    else:
        raise TranscriptionError(f'Unknown transcription mode: {mode}')


def _transcribe_cloud(audio_path, config):
    cfg = config['transcription']['cloud']
    api_key = os.environ.get(cfg.get('apiKeyEnv', 'OPENAI_API_KEY'))
    if not api_key:
        raise TranscriptionError(
            f'{cfg.get("apiKeyEnv", "OPENAI_API_KEY")} is not set in the environment.'
        )

    with open(audio_path, 'rb') as f:
        files = {'file': (os.path.basename(audio_path), f, 'audio/mpeg')}
        data = {
            'model': cfg.get('model', 'whisper-1'),
            'response_format': 'vtt',
        }
        headers = {'Authorization': f'Bearer {api_key}'}
        res = requests.post(OPENAI_TRANSCRIBE_URL, headers=headers, data=data,
                             files=files, timeout=600)
    if res.status_code != 200:
        raise TranscriptionError(f'OpenAI transcription failed ({res.status_code}): {res.text[:500]}')
    return res.text


def _transcribe_local(audio_path, config, dest_vtt_path):
    cfg = config['transcription']['local']
    with tempfile.TemporaryDirectory() as outdir:
        cmd_str = cfg['command'].format(input=audio_path, outdir=outdir)
        try:
            proc = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired as e:
            raise TranscriptionError(f'Local transcription timed out: {e}')
        if proc.returncode == 127:
            raise TranscriptionError(
                f'Local transcription command failed (exit 127 - command not found):\n{proc.stderr[-2000:]}\n'
                f'  Install the tool this command template expects (e.g. `pip install -U openai-whisper`), '
                f'make sure it\'s on PATH, or switch to cloud transcription in "Configure AI pipeline" > '
                f'"Transcription (Whisper)".'
            )
        if proc.returncode != 0:
            raise TranscriptionError(
                f'Local transcription command failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}'
            )

        base = os.path.splitext(os.path.basename(audio_path))[0]
        produced = os.path.join(outdir, f'{base}.vtt')
        if not os.path.isfile(produced):
            # Some tools name output after the whole path or lowercase differently;
            # fall back to searching for any .vtt file the command produced.
            candidates = [f for f in os.listdir(outdir) if f.endswith('.vtt')]
            if not candidates:
                raise TranscriptionError(
                    f'Local transcription command exited 0 but did not produce a .vtt file in '
                    f'{outdir} (files present: {os.listdir(outdir) or "none"}).\n'
                    f'stdout:\n{proc.stdout[-1000:]}\n'
                    f'stderr:\n{proc.stderr[-1000:]}'
                )
            produced = os.path.join(outdir, candidates[0])

        with open(produced, 'r', encoding='utf-8') as f:
            content = f.read()
        with open(dest_vtt_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return dest_vtt_path


def local_command_for(python_exe):
    """The default local transcription command, pointed at a specific
    python executable (e.g. a dedicated venv's, from install_local_whisper)."""
    return (f'{python_exe} -m whisper {{input}} --model base.en --language en '
            f'--output_format vtt --output_dir {{outdir}}')


def whisper_ready_for(python_exe):
    """True if openai-whisper (not the PyPI-name-colliding Graphite `whisper`
    package -- see check_ready) is importable and usable by python_exe."""
    check = subprocess.run(
        [python_exe, '-c', 'import whisper, sys; sys.exit(0 if hasattr(whisper, "load_model") else 1)'],
        capture_output=True, text=True)
    return check.returncode == 0


def install_local_whisper(base_dir):
    """Set up local transcription the low-pain way: a dedicated virtual
    environment (so no sudo/PEP-668 fights with the system Python, and no
    risk of the `pip install whisper` name collision) with the CPU-only
    PyTorch build (so it doesn't pull multi-GB CUDA/GPU packages a typical
    server has no use for), then openai-whisper on top.

    No-ops if that venv already has a working openai-whisper. Streams the
    installer's own output straight to the terminal, since these are large,
    slow downloads a user should be able to watch progress on. Returns the
    venv's python executable path. Raises TranscriptionError on failure.
    """
    venv_dir = os.path.join(base_dir, WHISPER_VENV_DIRNAME)
    python_exe = os.path.join(venv_dir, 'bin', 'python3')

    if os.path.isfile(python_exe) and whisper_ready_for(python_exe):
        print(f'  openai-whisper is already installed and working in {venv_dir}.')
        return python_exe

    if not os.path.isfile(python_exe):
        print(f'  Creating virtual environment at {venv_dir} ...')
        proc = subprocess.run([sys.executable, '-m', 'venv', venv_dir])
        if proc.returncode != 0 or not os.path.isfile(python_exe):
            raise TranscriptionError(
                f'Failed to create virtual environment at {venv_dir} (see output above). '
                f'On Debian/Ubuntu this usually means the `python3-venv` package isn\'t '
                f'installed -- try `sudo apt install python3-venv` and retry.'
            )

    print('  Installing CPU-only PyTorch (avoids pulling multi-GB CUDA/GPU packages '
          'a typical server has no use for) ...')
    proc = subprocess.run([python_exe, '-m', 'pip', 'install', '-q',
                            'torch', '--index-url', PYTORCH_CPU_INDEX_URL])
    if proc.returncode != 0:
        raise TranscriptionError('Failed to install PyTorch (CPU build); see output above.')

    print('  Installing openai-whisper ...')
    proc = subprocess.run([python_exe, '-m', 'pip', 'install', '-q', '-U', 'openai-whisper'])
    if proc.returncode != 0:
        raise TranscriptionError('Failed to install openai-whisper; see output above.')

    if not whisper_ready_for(python_exe):
        raise TranscriptionError(
            'openai-whisper installed but still doesn\'t look usable afterward -- something '
            'went wrong. Check the install output above.'
        )
    return python_exe
