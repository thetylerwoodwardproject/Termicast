import json
import os

DATA_FILE = os.path.join(os.path.dirname(__file__), 'podcast.json')

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
        'guid': '',
        'itunesType': 'episodic',
        'locked': 'no',
        'txt': [],
        'podroll': []
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
    return load()['episodes']


def get_episode(ep_id):
    return next((e for e in load()['episodes'] if e['id'] == ep_id), None)


def add_episode(episode):
    data = load()
    data['episodes'].append(episode)
    data['episodes'].sort(key=lambda e: e['pubDate'], reverse=True)
    save(data)


def update_episode(ep_id, updates):
    data = load()
    idx = next((i for i, e in enumerate(data['episodes']) if e['id'] == ep_id), None)
    if idx is None:
        raise ValueError(f'Episode {ep_id} not found')
    data['episodes'][idx].update(updates)
    data['episodes'].sort(key=lambda e: e['pubDate'], reverse=True)
    save(data)


def delete_episode(ep_id):
    data = load()
    idx = next((i for i, e in enumerate(data['episodes']) if e['id'] == ep_id), None)
    if idx is None:
        raise ValueError(f'Episode {ep_id} not found')
    data['episodes'].pop(idx)
    save(data)
