"""
pipeline_config.py - config file for the automated episode pipeline.

Everything that controls *how* Whisper transcribes, how Claude/OpenAI writes
titles/descriptions/keywords/chapters/soundbites/social posts, and how the
soundbite waveform videos look lives in pipeline.json. Edit it by hand or
through "Configure AI pipeline" in the CLI.
"""
import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'pipeline.json')

DEFAULT = {
    'transcription': {
        # 'cloud' -> OpenAI's hosted Whisper API. 'local' -> shell out to a
        # local whisper binary (openai-whisper's `whisper` CLI, whisper.cpp,
        # faster-whisper's CLI wrapper -- anything that takes an audio file
        # and writes a .vtt).
        'mode': 'cloud',
        'cloud': {
            'apiKeyEnv': 'OPENAI_API_KEY',
            'model': 'whisper-1',
        },
        'local': {
            # {input} and {outdir} are substituted. Must write "<basename>.vtt"
            # into {outdir}. Default matches the openai-whisper CLI, invoked via
            # `python3 -m whisper` rather than the bare `whisper` binary so it
            # keeps working even when the whisper console-script isn't on the
            # PATH the pipeline runs with (a common issue after `pip install
            # --user`/system-wide installs) -- as long as `python3` can import
            # the whisper module, this command finds it.
            'command': 'python3 -m whisper {input} --model base.en --language en '
                       '--output_format vtt --output_dir {outdir}',
        },
    },
    'llm': {
        # 'claude' or 'openai'
        'provider': 'claude',
        'claude': {
            'apiKeyEnv': 'ANTHROPIC_API_KEY',
            'model': 'claude-sonnet-4-5',
        },
        'openai': {
            'apiKeyEnv': 'OPENAI_API_KEY',
            'model': 'gpt-4o',
        },
    },
    # Instructions handed to the LLM for each generation step. Edit these to
    # control tone, style, and any rules specific to your show.
    'prompts': {
        'tone': 'Conversational and direct. No corporate speak, no clickbait.',
        'titles': 'Write 3 distinct, SEO-friendly episode titles under 70 characters each.',
        'description': 'Write an SEO-ready episode description using simple HTML '
                        '(<p>, <b>, <a>) suitable for modern podcast directories. '
                        '2-4 short paragraphs.',
        'keywords': 'Generate 10 SEO keywords/phrases for the podcast RSS feed, '
                     'ordered most to least important.',
        'chapters': 'Break the episode into logical chapters based on topic changes. '
                     'Each chapter needs a short, descriptive title.',
        'soundbites': 'Pick the most compelling, self-contained standout moments that '
                       'would work as short social clips. Prefer complete thoughts.',
        'socialPosts': 'Write 3 social media posts teasing the episode, casual tone, '
                        'each with 4-5 relevant hashtags.',
    },
    'soundbites': {
        'count': 5,
        'minDurationSeconds': 8,
        'maxDurationSeconds': 60,
        'maxTotalDurationSeconds': 180,
        'titleMaxChars': 70,
    },
    'video': {
        'enabled': True,
        'width': 1080,
        'height': 1920,
        'fps': 24,
        # RED, GREEN, or BLUE -- solid chroma-key background for compositing
        # over another image/video in Canva (shorts/reels/TikTok).
        'bgColor': 'GREEN',
        'waveformColorHex': '#FF4500',
        'paddingRatio': 0.06,
    },
    'schedule': {
        'recurring': {
            'enabled': False,
            # Mon/Tue/Wed/Thu/Fri/Sat/Sun
            'dayOfWeek': 'Tue',
            'time': '05:00',
            'timezone': 'UTC',
        },
    },
    'outputFormat': 'mp3',
    'workDir': os.path.join(os.path.dirname(__file__), 'pipeline_work'),
    'editorialDir': os.path.join(os.path.dirname(__file__), 'editorial'),
    # Default folder "Process New Episode" reads audio/artwork from. Blank
    # means always ask. The source audio/artwork files here are deleted once
    # they've been converted/copied into ./media/ -- keep your own backup of
    # originals if you want one.
    'inputDir': '',
}

WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# Previous default for transcription.local.command, before it switched to
# invoking `python3 -m whisper` instead of the bare `whisper` binary (see
# DEFAULT above). Used by load() to auto-upgrade a pipeline.json that still
# has this exact untouched default, without clobbering a command someone
# customized by hand.
_OLD_DEFAULT_LOCAL_COMMAND = ('whisper {input} --model base.en --language en '
                               '--output_format vtt --output_dir {outdir}')


def _deep_merge(base, overrides):
    result = dict(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load():
    if not os.path.exists(CONFIG_FILE):
        save(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    with open(CONFIG_FILE, 'r') as f:
        raw = json.load(f)
    config = _deep_merge(DEFAULT, raw)

    if config['transcription']['local']['command'] == _OLD_DEFAULT_LOCAL_COMMAND:
        config['transcription']['local']['command'] = DEFAULT['transcription']['local']['command']
        save(config)

    return config


def save(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def is_first_run():
    return not os.path.exists(CONFIG_FILE)
