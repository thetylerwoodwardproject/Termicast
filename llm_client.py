"""
llm_client.py - unified Claude/OpenAI text generation for the pipeline.

Every generation step (titles, description, keywords, chapters, soundbite
selection, social posts) goes through generate_json(), which sends a system
prompt + user prompt to whichever provider pipeline.json's llm.provider
selects, and parses the JSON object/array out of the reply.
"""
import json
import re

import requests

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
OPENAI_CHAT_URL = 'https://api.openai.com/v1/chat/completions'

_ENV_VAR_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class LLMError(Exception):
    pass


def _api_key(env_name):
    import os
    key = os.environ.get(env_name)
    if not key:
        msg = f'{env_name} is not set in the environment.'
        if not _ENV_VAR_NAME_RE.match(env_name):
            msg += (" That doesn't look like an environment variable name -- pipeline.json's "
                     "apiKeyEnv setting looks like it has the API key itself in it instead of "
                     "the variable name. Run \"Configure AI pipeline\" and enter the name of "
                     "the environment variable that holds the key (e.g. ANTHROPIC_API_KEY), "
                     "not the key.")
        raise LLMError(msg)
    return key


def _call_claude(system, user, config):
    cfg = config['llm']['claude']
    key = _api_key(cfg.get('apiKeyEnv', 'ANTHROPIC_API_KEY'))
    headers = {
        'x-api-key': key,
        'anthropic-version': ANTHROPIC_VERSION,
        'content-type': 'application/json',
    }
    payload = {
        'model': cfg.get('model', 'claude-sonnet-4-5'),
        'max_tokens': 4096,
        'system': system,
        'messages': [{'role': 'user', 'content': user}],
    }
    res = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=180)
    if res.status_code != 200:
        raise LLMError(f'Claude request failed ({res.status_code}): {res.text[:500]}')
    data = res.json()
    return ''.join(block.get('text', '') for block in data.get('content', []))


def _call_openai(system, user, config):
    cfg = config['llm']['openai']
    key = _api_key(cfg.get('apiKeyEnv', 'OPENAI_API_KEY'))
    headers = {'Authorization': f'Bearer {key}', 'content-type': 'application/json'}
    payload = {
        'model': cfg.get('model', 'gpt-4o'),
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
    }
    res = requests.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=180)
    if res.status_code != 200:
        raise LLMError(f'OpenAI request failed ({res.status_code}): {res.text[:500]}')
    data = res.json()
    return data['choices'][0]['message']['content']


def generate_text(system, user, config):
    provider = config['llm']['provider']
    if provider == 'claude':
        return _call_claude(system, user, config)
    elif provider == 'openai':
        return _call_openai(system, user, config)
    else:
        raise LLMError(f'Unknown llm provider: {provider}')


def _extract_json(text):
    """Pull the first JSON object/array out of a reply that may have prose around it."""
    text = text.strip()
    # Strip markdown code fences if present
    fence = re.match(r'^```(?:json)?\s*(.*?)\s*```$', text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to grabbing the outermost {...} or [...] block
    for open_ch, close_ch in (('{', '}'), ('[', ']')):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f'Could not parse JSON from LLM reply:\n{text[:1000]}')


def generate_json(system, user, config):
    reply = generate_text(system, user, config)
    return _extract_json(reply)


# ── Generation steps ─────────────────────────────────────────────────────────

def _base_system(config, extra=''):
    tone = config['prompts'].get('tone', '')
    return f'You are writing metadata for a podcast episode. Tone: {tone}\n{extra}'.strip()


def generate_titles(transcript_text, config):
    system = _base_system(config, config['prompts']['titles'] +
                           '\nRespond with ONLY a JSON array of exactly 3 strings, no commentary.')
    user = f'Episode transcript:\n\n{transcript_text[:12000]}'
    result = generate_json(system, user, config)
    if not isinstance(result, list) or len(result) < 1:
        raise LLMError(f'Expected a JSON array of titles, got: {result!r}')
    return [str(t) for t in result][:3]


def generate_description(transcript_text, titles_chosen, config):
    system = _base_system(config, config['prompts']['description'] +
                           '\nRespond with ONLY a JSON object: {"description": "<html>"}.')
    user = f'Episode title: {titles_chosen}\n\nTranscript:\n\n{transcript_text[:12000]}'
    result = generate_json(system, user, config)
    return str(result.get('description', '')) if isinstance(result, dict) else str(result)


def generate_keywords(transcript_text, config):
    system = _base_system(config, config['prompts']['keywords'] +
                           '\nRespond with ONLY a JSON array of exactly 10 short strings, no commentary.')
    user = f'Transcript:\n\n{transcript_text[:12000]}'
    result = generate_json(system, user, config)
    if not isinstance(result, list):
        raise LLMError(f'Expected a JSON array of keywords, got: {result!r}')
    return [str(k) for k in result][:10]


def generate_chapters(transcript_with_ts, config):
    system = _base_system(config, config['prompts']['chapters'] +
                           '\nRespond with ONLY a JSON array of objects: '
                           '{"startTime": <seconds:number>, "endTime": <seconds:number>, "title": "<string>"}. '
                           'Start with startTime 0. Cover the entire episode duration with no gaps.')
    user = f'Transcript with timestamps:\n\n{transcript_with_ts[:16000]}'
    result = generate_json(system, user, config)
    if not isinstance(result, list):
        raise LLMError(f'Expected a JSON array of chapters, got: {result!r}')
    chapters = []
    for ch in result:
        if not isinstance(ch, dict) or 'title' not in ch or 'startTime' not in ch:
            continue
        chapters.append({
            'startTime': float(ch['startTime']),
            'endTime': float(ch['endTime']) if ch.get('endTime') is not None else None,
            'title': str(ch['title']),
        })
    chapters.sort(key=lambda c: c['startTime'])
    return chapters


def generate_soundbites(transcript_with_ts, count, min_dur, max_dur, config):
    system = _base_system(config, config['prompts']['soundbites'] +
                           f'\nPick up to {count} candidates. Each clip must be between '
                           f'{min_dur} and {max_dur} seconds long. '
                           '\nRespond with ONLY a JSON array of objects: '
                           '{"startTime": <seconds:number>, "endTime": <seconds:number>, "title": "<short title>"}.')
    user = f'Transcript with timestamps:\n\n{transcript_with_ts[:16000]}'
    result = generate_json(system, user, config)
    if not isinstance(result, list):
        raise LLMError(f'Expected a JSON array of soundbites, got: {result!r}')
    soundbites = []
    for sb in result:
        if not isinstance(sb, dict) or 'startTime' not in sb or 'endTime' not in sb:
            continue
        soundbites.append({
            'startTime': float(sb['startTime']),
            'endTime': float(sb['endTime']),
            'title': str(sb.get('title', '')).strip(),
        })
    return soundbites


def generate_social_posts(transcript_text, title, description_plain, config):
    system = _base_system(config, config['prompts']['socialPosts'] +
                           '\nEach post must be 280 characters or fewer INCLUDING hashtags, '
                           'and include 4-5 relevant hashtags. '
                           '\nRespond with ONLY a JSON array of exactly 3 strings, no commentary.')
    user = (f'Episode title: {title}\n\nDescription: {description_plain[:1000]}\n\n'
            f'Transcript excerpt:\n\n{transcript_text[:6000]}')
    result = generate_json(system, user, config)
    if not isinstance(result, list):
        raise LLMError(f'Expected a JSON array of social posts, got: {result!r}')
    return [str(p) for p in result][:3]
