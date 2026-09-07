"""
pipeline.py - shared helpers for the automated AI episode pipeline:
slugging, recurring-schedule slot calculation, soundbite constraint
enforcement, and the editorial markdown writer.

The interactive orchestration (prompting the user, calling transcribe.py /
llm_client.py / audio_tools.py in sequence) lives in cli.py, same as how
add_episode()/manage_chapters() work today -- this module holds the parts
that are pure logic and worth testing/reusing on their own.
"""
import os
import re
from datetime import datetime, timedelta, timezone

import pipeline_config

WEEKDAY_INDEX = {name: i for i, name in enumerate(pipeline_config.WEEKDAYS)}


def slugify(text, max_len=None):
    text = re.sub(r"[^\w\s-]", '', text or '').strip().lower()
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-{2,}', '-', text).strip('-')
    if max_len:
        text = text[:max_len].rstrip('-')
    return text or 'untitled'


def date_prefix(pub_date_iso):
    dt = datetime.fromisoformat(pub_date_iso.replace('Z', '+00:00'))
    return dt.strftime('%y%m%d')


def seconds_to_hms(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


# ── Recurring schedule ───────────────────────────────────────────────────────

def next_recurring_slot(config, existing_pubdates=None, after=None):
    """
    Return the next UTC ISO timestamp matching config's recurring schedule
    (e.g. every Tuesday at 05:00) that falls strictly after `after` (default:
    now) AND strictly after the latest of existing_pubdates -- so batches of
    episodes processed together queue up one slot per week instead of piling
    onto the same date.
    """
    sched = config['schedule']['recurring']
    if not sched.get('enabled'):
        raise ValueError('Recurring schedule is not enabled in pipeline.json')

    target_weekday = WEEKDAY_INDEX[sched['dayOfWeek']]
    hh, mm = (int(x) for x in sched['time'].split(':'))

    now = after or datetime.now(timezone.utc)
    baseline = now
    if existing_pubdates:
        latest = max(
            datetime.fromisoformat(d.replace('Z', '+00:00')) for d in existing_pubdates
        )
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        baseline = max(baseline, latest)

    candidate = baseline.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days_ahead = (target_weekday - candidate.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    if candidate <= baseline:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc).isoformat()


# ── Soundbite constraint enforcement ─────────────────────────────────────────

def finalize_soundbites(candidates, total_duration, sb_config):
    """Clamp LLM-picked soundbite candidates to the configured count, per-clip
    duration bounds, and total combined duration cap."""
    count = sb_config.get('count', 5)
    min_dur = sb_config.get('minDurationSeconds', 8)
    max_dur = sb_config.get('maxDurationSeconds', 60)
    max_total = sb_config.get('maxTotalDurationSeconds', 180)
    title_max = sb_config.get('titleMaxChars', 70)

    finalized = []
    running_total = 0.0
    for c in candidates:
        start = max(0.0, float(c['startTime']))
        end = min(float(total_duration), float(c['endTime']))
        if end <= start:
            continue
        dur = end - start
        if dur < min_dur:
            continue
        if dur > max_dur:
            end = start + max_dur
            dur = max_dur
        if running_total + dur > max_total:
            continue
        title = str(c.get('title', '')).strip()[:title_max]
        finalized.append({'startTime': start, 'endTime': end, 'duration': dur, 'title': title})
        running_total += dur
        if len(finalized) >= count:
            break
    return finalized


# ── Editorial markdown ───────────────────────────────────────────────────────

def write_editorial_markdown(episode, config):
    """
    Write the YYMMDD_EPISODE-TITLE.md editorial file for a processed episode
    and return its path. Safe to call again to regenerate after edits.
    """
    editorial_dir = config['editorialDir']
    os.makedirs(editorial_dir, exist_ok=True)

    prefix = date_prefix(episode['pubDate'])
    title = episode['titleOptions'][episode['titleChosenIndex']]
    filename = f"{prefix}_{slugify(title, max_len=60)}.md"
    path = os.path.join(editorial_dir, filename)

    lines = [f'# {title}', '']
    lines.append('## Titles')
    for i, t in enumerate(episode.get('titleOptions') or []):
        marker = ' *' if i == episode.get('titleChosenIndex') else ''
        lines.append(f'- {t}{marker}')
    lines.append('')

    lines.append('## Description')
    lines.append(episode.get('description', ''))
    lines.append('')

    lines.append('## Keywords')
    lines.append(', '.join(episode.get('keywords') or []))
    lines.append('')

    lines.append('## Chapters')
    for ch in episode.get('chapters') or []:
        start = seconds_to_hms(ch['startTime'])
        end = seconds_to_hms(ch['endTime']) if ch.get('endTime') is not None else '?'
        lines.append(f'- [{start} - {end}] {ch["title"]}')
    lines.append('')

    lines.append('## Soundbites')
    for sb in episode.get('soundbites') or []:
        lines.append(f'### {sb["title"]}')
        lines.append(f'- MP3: `{sb.get("mp3", "")}`')
        lines.append(f'- MP4: `{sb.get("mp4", "")}`')
        lines.append(f'- Time: {seconds_to_hms(sb["startTime"])} - {seconds_to_hms(sb["endTime"])}')
        if sb.get('caption'):
            lines.append(f'- Caption: "{sb["caption"]}"')
        lines.append('')

    lines.append('## Social Media Posts')
    for i, post in enumerate(episode.get('socialPosts') or [], 1):
        lines.append(f'{i}. {post}')
    lines.append('')

    content = '\n'.join(lines)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path
