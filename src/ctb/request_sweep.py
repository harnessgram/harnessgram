"""Bounded background reconciliation using the request ledger lock and format."""
import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from . import owner_requests

EVERY = 600
CACHE_ITEMS = 1000
MAX_CALLS = 30
_guard = threading.Lock()
_last = None
_cursor = 0
_VERDICT = re.compile(r":\s*(ship|fix-first)\b", re.I)
_URL = re.compile(r"https://" + r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(pull|issues)/([1-9][0-9]*)/?(?![A-Za-z0-9_/?#%-]|\.[A-Za-z0-9])")


def item_urls(item):
    """Return explicit completion links only; reject any unsupported criterion."""
    links = item.get('links')
    if not isinstance(links, list) or not links or len(links) > MAX_CALLS:
        return {}
    matches = {}
    for link in links:
        match = _URL.fullmatch(link) if isinstance(link, str) else None
        if not match:
            return {}
        owner, repo, kind, number = match.groups()
        canonical = f'https://github.com/{owner.lower()}/{repo.lower()}/{kind}/{number}'
        matches[canonical] = _URL.fullmatch(canonical)
    return matches


def change(path, ident, mutate):
    # request ledger locks <project>.lock, not the inode replaced by os.replace.
    with open(path.with_suffix('.lock'), 'a') as lock:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('request ledger busy') from None
                time.sleep(min(0.05, remaining))
        data = owner_requests._read(path)
        items = data.get('items') if isinstance(data, dict) else None
        hits = [i for i in items if isinstance(i, dict) and type(i.get('id')) is int and i['id'] == ident] if isinstance(items, list) else []
        if len(hits) != 1 or hits[0].get('status') != 'open' or not mutate(hits[0]):
            return False
        fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True


def touch(path, ident, now=None):
    def update(item):
        if item.get('wait') != 'owner':
            return False
        item['updated'] = time.time() if now is None else now
        return True
    return change(path, ident, update)


def github_state(match):
    owner, repo, kind, number = match.groups()
    endpoint = f"repos/{owner}/{repo}/{'pulls' if kind == 'pull' else 'issues'}/{number}"
    result = subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True, timeout=5, check=True)
    state = json.loads(result.stdout)
    if kind == 'pull' and state.get('state') == 'open':
        # Commit dates are not push times. The REST PR response does not
        # establish when this head was pushed; require an explicit SHA verdict
        # unless a caller supplies a proven head_pushed_at timestamp.
        if not state.get('head', {}).get('sha'):
            return dict(state, ship=False)
        # Flatten pages before selecting by time; a later fix-first verdict on
        # another page must supersede an earlier ship. Never cache comment text.
        result = subprocess.run(['gh', 'api', '--paginate',
                                 f'repos/{owner}/{repo}/issues/{number}/comments?per_page=100',
                                 '--slurp', '--jq',
                                 '[.[][] | {body: (.body // ""), created_at}]'],
                                capture_output=True, text=True, timeout=5, check=True)
        state['comments'] = json.loads(result.stdout)
        state['ship'] = shipped(state)
        del state['comments']
    return state


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.timestamp() if date.tzinfo is not None else None
    except ValueError:
        return None


def shipped(state):
    comments = state.get('comments')
    if not isinstance(comments, list):
        return state.get('ship') is True
    head_time = _timestamp(state.get('head_pushed_at'))
    sha = state.get('head', {}).get('sha', '').lower()
    verdicts = []
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get('body'), str):
            continue
        verdict = _VERDICT.search(comment['body'].split('\n', 1)[0])
        at = _timestamp(comment.get('created_at'))
        if verdict and at is not None:
            names_head = bool(sha) and any(
                sha.startswith(token.lower())
                for token in re.findall(r'(?<![\w])[0-9a-fA-F]{7,40}(?![\w])', comment['body']))
            current = names_head or (head_time is not None and at > head_time)
            verdicts.append((at, verdict.group(1).lower(), current))
    if not verdicts:
        return False
    latest = max(at for at, _, _ in verdicts)
    # Conflicting verdicts stamped in the same second are not a safe ship signal.
    return all(verdict == 'ship' and current for at, verdict, current in verdicts if at == latest)


def completed(state, kind):
    return (bool(state.get('merged_at')) if kind == 'pull' else
            state.get('state') == 'closed' and state.get('state_reason') == 'completed'
            and 'pull_request' not in state)


def _cache(folder):
    if folder is None:
        return {}
    path = folder / '.request-link-cache'
    try:
        if not path.is_file():
            return {}
        data = owner_requests._read(path)
        if not isinstance(data, dict) or len(data) > CACHE_ITEMS:
            return {}
        return {url: entry for url, entry in data.items()
                if _URL.fullmatch(url) and isinstance(entry, dict)
                and owner_requests._time(entry.get('at')) is not None
                and isinstance(entry.get('state'), dict)}
    except (OSError, ValueError, RecursionError):
        return {}


def cached_states(cfg):
    """Nonblocking, read-only snapshot for the panel, including last known state.

    Missing, stale or broken cache data never triggers a GitHub request here.
    """
    return {url: entry['state'] for url, entry in _cache(owner_requests.ledger_dir(cfg)).items()}


def _refresh_state(cache, url, match, now, fetch):
    """(state, fresh): fresh is False when this read failed and the cached state was kept."""
    if url not in cache and len(cache) >= CACHE_ITEMS:
        del cache[min(cache, key=lambda u: cache[u]['at'])]
    entry = cache.get(url, {})
    fresh = False
    try:
        state = fetch(match)
        if not isinstance(state, dict):
            raise ValueError('invalid link state')
        ship = shipped(state)
        state = {k: state[k] for k in ('state', 'state_reason', 'merged_at', 'draft', 'pull_request') if k in state}
        # Never persist comment text; fake readers use the same reduction.
        entry = {'state': dict(state, ship=ship)}
        fresh = True
    except Exception as exc:
        logging.getLogger(__name__).warning('Request link read failed (%s)', type(exc).__name__)
    cache[url] = {'at': now, 'state': entry.get('state', {})}
    return cache[url]['state'], fresh


def _write_cache(folder, cache):
    fd, tmp = tempfile.mkstemp(prefix='.request-link-cache.', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(cache, stream)
        os.replace(tmp, folder / '.request-link-cache')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sweep(cfg, now=None, fetch=github_state):
    global _cursor
    folder = owner_requests.ledger_dir(cfg)
    if folder is None:
        return
    # Explicit `now` is a fixed test clock. Background reads use the actual
    # attempt time after locking, not the start of a slow multi-link sweep.
    read_time = now
    now = time.time() if now is None else now
    jobs = []
    for path in sorted(folder.glob('*.json')):
        try:
            data = owner_requests._read(path)
            items = data.get('items') if isinstance(data, dict) else None
            for item in (items if isinstance(items, list) else [])[:owner_requests.MAX_ITEMS]:
                if not isinstance(item, dict) or item.get('status') != 'open' or type(item.get('id')) is not int:
                    continue
                links = item_urls(item)
                if links:
                    jobs.append((path, item['id'], links))
        except (OSError, ValueError, RecursionError):
            logging.getLogger(__name__).warning('Request ledger could not be read')
    if not jobs:
        return
    cache = _cache(folder)
    refreshed = {}
    start = _cursor % len(jobs)
    calls = 0
    for offset in range(len(jobs)):
        path, ident, links = jobs[(start + offset) % len(jobs)]
        if calls + len(links) > MAX_CALLS:
            break
        calls += len(links)
        _cursor = (start + offset + 1) % len(jobs)
        try:
            states = []
            for url, match in links.items():
                if url not in refreshed:
                    at = time.time() if read_time is None else read_time
                    refreshed[url] = _refresh_state(cache, url, match, at, fetch)
                states.append((match, *refreshed[url]))
            # Close only from reads that succeeded in this sweep, never from a cached state after a failure.
            if all(fresh and completed(state, match.group(3)) for match, state, fresh in states):
                def close(item):
                    if list(item_urls(item)) != list(links) or not isinstance(item.get('notes'), list):
                        return False
                    item['status'] = 'done'
                    item['updated'] = time.time() if now is None else now
                    if len(links) == 1:
                        match = next(iter(links.values()))
                        kind, number = match.group(3), match.group(4)
                        note = f"closed automatically: {'PR' if kind == 'pull' else 'issue'} #{number} {'merged' if kind == 'pull' else 'closed'}"
                    else:
                        note = 'closed automatically: all linked PRs and issues completed'
                    item['notes'].append(note)
                    return True
                change(path, ident, close)
        except Exception as exc:
            logging.getLogger(__name__).warning('Request reconciliation failed (%s)', type(exc).__name__)
    # Panel readers retain the previous snapshot until the sweep finishes.
    _write_cache(folder, cache)


def start(cfg, now=None):
    global _last
    now = time.time() if now is None else now
    if not _guard.acquire(blocking=False):
        return
    if _last is not None and now - _last < EVERY:
        _guard.release()
        return
    _last = now
    def run():
        try:
            folder = owner_requests.ledger_dir(cfg)
            if folder is None or not folder.is_dir():
                return
            # Serialize across bridge processes and remember the start across restarts.
            with open(folder / '.request-sweep.lock', 'a+') as stamp:
                try:
                    fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                stamp.seek(0)
                try:
                    previous = float(stamp.read().strip())
                except ValueError:
                    previous = 0
                if previous and now - previous < EVERY:
                    return
                stamp.seek(0)
                stamp.truncate()
                stamp.write(str(now))
                stamp.flush()
                sweep(cfg)
        except Exception as exc:
            logging.getLogger(__name__).warning('Request sweep failed (%s)', type(exc).__name__)
        finally:
            _guard.release()
    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _guard.release()
        raise
