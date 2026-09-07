"""
vtt_utils.py - minimal WebVTT parsing shared by the LLM prompt builder and
the soundbite/chapter extraction steps of the pipeline.
"""
import re

TIMESTAMP_RE = re.compile(
    r'(\d{2}:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}:)?(\d{2}):(\d{2})[.,](\d{3})'
)


def _ts_to_seconds(hh, mm, ss, ms):
    hh = int(hh[:-1]) if hh else 0
    return hh * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_vtt(path):
    """Return a list of {'start': float, 'end': float, 'text': str} cues."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    cues = []
    blocks = re.split(r'\n\s*\n', content.replace('\r\n', '\n'))
    for block in blocks:
        lines = [l for l in block.split('\n') if l.strip()]
        if not lines:
            continue
        ts_line_idx = None
        for i, line in enumerate(lines):
            if '-->' in line:
                ts_line_idx = i
                break
        if ts_line_idx is None:
            continue
        m = TIMESTAMP_RE.search(lines[ts_line_idx])
        if not m:
            continue
        start = _ts_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ts_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        text = ' '.join(lines[ts_line_idx + 1:]).strip()
        text = re.sub(r'<[^>]+>', '', text)  # strip any inline VTT tags
        if text:
            cues.append({'start': start, 'end': end, 'text': text})
    return cues


def full_text(cues):
    return ' '.join(c['text'] for c in cues)


def transcript_with_timestamps(cues):
    """Render cues as 'MM:SS text' lines for feeding to an LLM."""
    lines = []
    for c in cues:
        m, s = divmod(int(c['start']), 60)
        h, m = divmod(m, 60)
        ts = f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'
        lines.append(f'[{ts}] {c["text"]}')
    return '\n'.join(lines)


def text_between(cues, start, end):
    """Concatenate cue text overlapping [start, end]."""
    parts = [c['text'] for c in cues if c['end'] > start and c['start'] < end]
    return ' '.join(parts).strip()


def duration(cues):
    return cues[-1]['end'] if cues else 0.0
