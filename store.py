import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BASE_DIR = os.environ.get('TERMICAST_DATA_DIR') or os.path.dirname(__file__)
DATA_FILE = os.path.join(BASE_DIR, 'podcast.json')

DEFAULT = {
    'show': {
        'title': '',
        'description': '',
        'author': '',
        'email': '',
        'category': '',
        'subcategory': '',
        'language': 'en',
        'artwork': '',
        'explicit': False,
        'link': '',
        'baseUrl': '',
        'op3Prefix': 'https://op3.dev/e/',
        'guid': ''
    },
    'episodes': []
}


def is_first_run():
    """True if podcast.json does not exist yet."""
    return not os.path.exists(DATA_FILE)


def load():
    if not os.path.exists(DATA_FILE):
        save(DEFAULT)
        return json.loads(json.dumps(DEFAULT))
    with open(DATA_FILE, 'r') as f:
        return json.load(f)


def save(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_show():
    return load()['show']


def save_show(updates):
    data = load()
    data['show'].update(updates)
    save(data)


def get_episodes():
    return sorted(load()['episodes'], key=episode_date, reverse=True)


def episode_date(episode):
    """Compare publication instants; legacy timezone-free dates are UTC."""
    dt = datetime.fromisoformat(episode['pubDate'].replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_rss_date(raw):
    dt = parsedate_to_datetime(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def get_episode(ep_id):
    return next((e for e in load()['episodes'] if e['id'] == ep_id), None)


def add_episode(episode):
    data = load()
    data['episodes'].append(episode)
    data['episodes'].sort(key=episode_date, reverse=True)
    save(data)


def update_episode(ep_id, updates):
    data = load()
    idx = next((i for i, e in enumerate(data['episodes']) if e['id'] == ep_id), None)
    if idx is None:
        raise ValueError(f'Episode {ep_id} not found')
    data['episodes'][idx].update(updates)
    data['episodes'].sort(key=episode_date, reverse=True)
    save(data)


def delete_episode(ep_id):
    data = load()
    idx = next((i for i, e in enumerate(data['episodes']) if e['id'] == ep_id), None)
    if idx is None:
        raise ValueError(f'Episode {ep_id} not found')
    data['episodes'].pop(idx)
    save(data)
