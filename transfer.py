"""Streaming downloads with atomic publication and optional OP3 recovery."""
import os
import tempfile
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests

from display import color, Progress


USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 '
              'Firefox/128.0 termicast-mirror/1.0')


def _op3_origin(url):
    """Unwrap only OP3's /e[/,flags] endpoint, not lookalike hosts."""
    try:
        outer = urlsplit(url)
        if (outer.scheme not in ('http', 'https') or outer.hostname != 'op3.dev'
                or outer.username is not None or outer.password is not None
                or outer.port not in (None, 80, 443)):
            return None
        endpoint, separator, target = outer.path[1:].partition('/')
        if not separator or not (endpoint == 'e' or endpoint.startswith('e,')):
            return None
        if not target or target.startswith('/'):
            return None
        if not target.startswith(('https://', 'http://')):
            if '://' in target:
                return None
            target = 'https://' + target
        origin = urlsplit(target)
        if (not origin.hostname or origin.username is not None
                or origin.password is not None or origin.hostname == 'op3.dev'
                or any(c.isspace() or c == '\\' for c in target)):
            return None
        origin.port  # Validate malformed ports before attempting a fallback.
        return urlunsplit((origin.scheme, origin.netloc, origin.path,
                           outer.query, outer.fragment))
    except ValueError:
        return None


def _triton_fallback(url):
    """Use only the same-host static fallback advertised by RSS.com's variant."""
    try:
        variant = urlsplit(url)
        if (variant.scheme != 'https' or variant.hostname != 'rsscom.pdn.tritondigital.com'
                or not variant.path.startswith('/v1/variant/')
                or variant.username is not None or variant.password is not None
                or variant.port not in (None, 443)):
            return None
        fallback = parse_qs(variant.query).get('fallback_url', [''])[0]
        target = urlsplit(fallback)
        if (target.scheme != 'https' or target.hostname != variant.hostname
                or target.username is not None or target.password is not None
                or target.port not in (None, 443) or target.path.startswith('/v1/')
                or not target.path.lower().endswith(('.mp3', '.m4a', '.ogg', '.wav', '.aac'))
                or any(c.isspace() or c == '\\' for c in fallback)):
            return None
        return fallback
    except ValueError:
        return None


def download(url, dest_path, log=print, label='', attempts=3):
    """Return True after atomically replacing dest_path with a valid download.

    Never skips an existing destination. Each URL gets at most attempts tries;
    an OP3 URL gets a direct-origin fallback only after those tries fail.
    Backoff is 0.5, 1, 2, then at most 4 seconds; requests time out after 60
    seconds of inactivity. Interrupts propagate after resource cleanup.
    """
    if attempts < 1:
        return False
    dest_path = os.fspath(dest_path)
    parent = os.path.dirname(os.path.abspath(dest_path))
    origin = _op3_origin(url)
    candidates = [url, origin] if origin else [url]
    static_fallback = None
    for candidate in candidates:
        if candidate == static_fallback:
            log(color('    Dynamic audio failed; trying the host-advertised static audio fallback.', '33'))
        elif candidate != url:
            log(color(f'    OP3 failed; retrying direct origin: {candidate}', '33'))
        for attempt in range(attempts):
            tmp_path = None
            progress = None
            advertised = None
            try:
                with requests.get(candidate, stream=True, timeout=60,
                                  headers={'User-Agent': USER_AGENT,
                                            'Accept-Encoding': 'identity'}) as response:
                    advertised = _triton_fallback(getattr(response, 'url', ''))
                    response.raise_for_status()
                    length = response.headers.get('Content-Length')
                    identity = response.headers.get('Content-Encoding', 'identity').strip().lower() in ('', 'identity')
                    total = int(length) if identity and length is not None else None
                    if total is not None and total < 0:
                        raise ValueError('negative Content-Length')
                    progress = Progress(total, label or os.path.basename(dest_path), log)
                    os.makedirs(parent, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode='wb', dir=parent,
                                                     prefix='.' + os.path.basename(dest_path) + '.',
                                                     suffix='.part', delete=False) as output:
                        tmp_path = output.name
                        size = 0
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                output.write(chunk)
                                size += len(chunk)
                                progress.update(size)
                        if not size:
                            raise ValueError('empty download')
                        if total is not None and size != total:
                            raise ValueError(f'Content-Length mismatch: expected {total}, received {size}')
                # Assets must be readable by the static web server, not just the CLI user.
                os.chmod(tmp_path, 0o644)
                os.replace(tmp_path, dest_path)
                tmp_path = None
                progress.finish()
                return True
            except PermissionError as error:
                if progress:
                    progress.close()
                log(color(f'    Cannot write download: {error}', '31'))
                return False
            except (requests.RequestException, OSError, ValueError) as error:
                if progress:
                    progress.close()
                log(color(f'    FAILED {candidate} ({attempt + 1}/{attempts}): {error}', '31'))
                if advertised and static_fallback is None:
                    # Retrying the OP3/origin URL reaches the same broken variant.
                    # Try its signed static audio once per sync, with normal retries.
                    static_fallback = advertised
                    candidates.insert(candidates.index(candidate) + 1, static_fallback)
                    break
            finally:
                if progress:
                    progress.close()
                if tmp_path is not None:
                    os.unlink(tmp_path)
            if attempt + 1 < attempts:
                time.sleep(min(0.5 * 2 ** min(attempt, 3), 4))
    return False
