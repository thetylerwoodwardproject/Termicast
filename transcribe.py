"""
transcribe.py - turn an episode's audio into a VTT transcript.

Two backends, picked by pipeline.json's transcription.mode:
  - 'cloud': OpenAI's hosted Whisper API (https://api.openai.com/v1/audio/transcriptions)
  - 'local': shells out to a local whisper binary (openai-whisper's `whisper`
             CLI, whisper.cpp, faster-whisper-cli...) using a configurable
             command template that must write "<basename>.vtt" into --outdir.
"""
import os
import subprocess
import tempfile

import requests

OPENAI_TRANSCRIBE_URL = 'https://api.openai.com/v1/audio/transcriptions'


class TranscriptionError(Exception):
    pass


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
                    f'Local transcription command did not produce a .vtt file in {outdir}. '
                    f'stdout:\n{proc.stdout[-1000:]}'
                )
            produced = os.path.join(outdir, candidates[0])

        with open(produced, 'r', encoding='utf-8') as f:
            content = f.read()
        with open(dest_vtt_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return dest_vtt_path
