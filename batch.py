"""Batch clipping from the command line (added in this fork).

Give it many links (or a text file of links). It sends each one to the NeonClipping app that is already running on
this computer, waits for the clips, and saves every clip with its captions and a spreadsheet of titles, hooks and
scores. Run it again with the same --out folder and it skips links that are already done, so a long list can be
stopped and continued.

    python batch.py https://youtu.be/abc https://youtu.be/def
    python batch.py --file links.txt --count 6 --min 20 --max 45 --out my-clips
    python batch.py --file links.txt --layout blur --style gta --language hinglish

links.txt: one link per line; empty lines and lines starting with # are ignored.
Start NeonClipping first (run.ps1 / run.sh or the desktop shortcut). Only the Python standard library is used.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SERVER = os.environ.get('NEONCLIPPING_URL', 'http://127.0.0.1:' + os.environ.get('NEONCLIPPING_PORT', '8770'))
MANIFEST = 'batch-done.json'


class BatchError(RuntimeError):
    pass


def read_links(args_links, file=None):
    """Links from the command line and/or a file, in order, without duplicates."""
    links = list(args_links or [])
    if file:
        for line in Path(file).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                links.append(line)
    seen, out = set(), []
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    bad = [l for l in out if not re.match(r'^https?://', l)]
    if bad:
        raise BatchError('These are not links (they must start with http:// or https://): ' + ', '.join(bad[:5]))
    return out


def safe_name(text, fallback='clip', limit=60):
    """A file or folder name that works on Windows, macOS and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', str(text or '')).strip()
    name = re.sub(r'\s+', ' ', name).strip(' .')[:limit].strip(' .')
    if name.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(?i)(COM|LPT)\d', name):
        name = '_' + name
    return name or fallback


def make_body(link, opts):
    """The request the app's own "Make clips" button sends, from the command-line options."""
    body = {'url': link, 'count': opts.count, 'min_len': opts.min, 'max_len': opts.max, 'mode': opts.mode,
            'layout': opts.layout, 'caption_style': opts.style, 'aspect': 'vertical', 'resolution': opts.resolution,
            'source_height': opts.source_height}
    if opts.language:
        body['language'] = opts.language
    return body


class Client:
    def __init__(self, server=DEFAULT_SERVER, timeout=60):
        self.server = server.rstrip('/')
        self.timeout = timeout

    def _open(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode() or '{}').get('detail')
            except ValueError:
                detail = None
            raise BatchError(f'{path}: {detail or "HTTP " + str(e.code)}')
        except (urllib.error.URLError, OSError) as e:
            raise BatchError(f'Cannot reach NeonClipping at {self.server}. Start the app first. ({e})')

    def json(self, path, body=None):
        with self._open(path, body) as r:
            return json.loads(r.read().decode())

    def download(self, path, dest):
        tmp = Path(str(dest) + '.part')
        with self._open(path) as r, open(tmp, 'wb') as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, dest)
        return dest


def wait_for(client, job, poll=3.0, on_progress=None, sleep=time.sleep, max_wait=6 * 3600):
    """Poll /make/<job> until it is done or failed. Returns the final job state."""
    waited, last = 0.0, None
    while True:
        st = client.json(f'/make/{job}')
        line = (st.get('stage'), round(float(st.get('progress') or 0), 2))
        if on_progress and line != last:
            on_progress(st)
            last = line
        if st.get('status') in ('done', 'error'):
            return st
        if waited >= max_wait:
            raise BatchError(f'Job {job} still running after {max_wait // 3600} hours.')
        sleep(poll)
        waited += poll


CSV_FIELDS = ['n', 'title', 'hook', 'score', 'start', 'end', 'seconds', 'video', 'captions', 'error']


def save_clips(client, state, folder):
    """Download every finished clip and write clips.csv and clips.json. Returns the rows."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, c in enumerate(state.get('clips') or [], 1):
        title = c.get('title') or f'Clip {i}'
        row = {'n': i, 'title': title, 'hook': c.get('hook', ''), 'score': c.get('score'), 'start': c.get('start'),
               'end': c.get('end'), 'seconds': None, 'video': '', 'captions': '', 'error': c.get('error') or ''}
        try:
            row['seconds'] = round(float(c['end']) - float(c['start']), 1)
        except (KeyError, TypeError, ValueError):
            pass
        fid = str(c.get('file') or '')
        if not row['error'] and fid.isalnum():
            base = f'{i:02d} {safe_name(title)}'
            try:
                client.download(f'/media/{fid}', folder / (base + '.mp4'))
                row['video'] = base + '.mp4'
                if c.get('srt'):
                    client.download(f'/text/{fid}/srt', folder / (base + '.srt'))
                    row['captions'] = base + '.srt'
            except BatchError as e:
                row['error'] = str(e)
        rows.append(row)
    with open(folder / 'clips.csv', 'w', newline='', encoding='utf-8-sig') as f:   # utf-8-sig: Excel shows Hindi and emoji correctly
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    (folder / 'clips.json').write_text(json.dumps({'source': state.get('source'), 'title': state.get('title'), 'clips': rows}, indent=2, ensure_ascii=False), encoding='utf-8')
    return rows


def load_manifest(out):
    p = out / MANIFEST
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_manifest(out, manifest):
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')


def run(links, opts, client=None, log=print, sleep=time.sleep):
    client = client or Client(opts.server)
    out = Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out)
    summary = {'done': 0, 'skipped': 0, 'failed': 0, 'clips': 0}
    for n, link in enumerate(links, 1):
        if link in manifest and manifest[link].get('status') == 'done' and not opts.redo:
            log(f'[{n}/{len(links)}] already done, skipping: {link}')
            summary['skipped'] += 1
            continue
        log(f'[{n}/{len(links)}] {link}')
        try:
            job = client.json('/make', make_body(link, opts))['job']
            st = wait_for(client, job, poll=opts.poll, sleep=sleep,
                          on_progress=lambda s: log(f'    {int(float(s.get("progress") or 0) * 100):3d}%  {s.get("stage", "")}'))
            if st.get('status') == 'error':
                raise BatchError(st.get('error') or 'the app reported an error')
            folder = out / f'{n:02d} {safe_name(st.get("title"), fallback="video " + str(n))}'
            rows = save_clips(client, st, folder)
            ok = sum(1 for r in rows if r['video'])
            manifest[link] = {'status': 'done', 'folder': folder.name, 'clips': ok, 'at': time.strftime('%Y-%m-%d %H:%M')}
            summary['done'] += 1
            summary['clips'] += ok
            log(f'    saved {ok} clip(s) to {folder}')
        except BatchError as e:
            manifest[link] = {'status': 'failed', 'error': str(e)[-300:], 'at': time.strftime('%Y-%m-%d %H:%M')}
            summary['failed'] += 1
            log(f'    failed: {e}')
            if 'Cannot reach NeonClipping' in str(e):
                save_manifest(out, manifest)
                raise
        save_manifest(out, manifest)
    log(f'Finished: {summary["done"]} done, {summary["skipped"]} skipped, {summary["failed"]} failed, {summary["clips"]} clips saved in {out}')
    return summary


def parser():
    p = argparse.ArgumentParser(description='Clip many videos with the running NeonClipping app.')
    p.add_argument('links', nargs='*', help='video links')
    p.add_argument('--file', help='text file with one link per line')
    p.add_argument('--out', default='batch-clips', help='output folder (default: batch-clips)')
    p.add_argument('--count', type=int, default=4, help='clips per video, 1 to 12 (default 4)')
    p.add_argument('--min', type=float, default=30, help='shortest clip in seconds (default 30)')
    p.add_argument('--max', type=float, default=60, help='longest clip in seconds (default 60)')
    p.add_argument('--mode', choices=['fast', 'deep'], default='fast')
    p.add_argument('--layout', default='blur', help='layout name as in the app (default blur)')
    p.add_argument('--style', default='gta', help='caption style as in the app (default gta)')
    p.add_argument('--language', default='', help='caption language, e.g. hinglish; empty = auto')
    p.add_argument('--resolution', type=int, default=1080)
    p.add_argument('--source-height', type=int, default=1080, dest='source_height')
    p.add_argument('--server', default=DEFAULT_SERVER, help='app address (default %(default)s)')
    p.add_argument('--poll', type=float, default=3.0, help='seconds between status checks')
    p.add_argument('--redo', action='store_true', help='clip links again even if they are already done')
    return p


def main(argv=None):
    opts = parser().parse_args(argv)
    try:
        links = read_links(opts.links, opts.file)
        if not links:
            raise BatchError('No links. Pass links or --file links.txt')
        if not 1 <= opts.count <= 12:
            raise BatchError('--count must be 1 to 12')
        if opts.min > opts.max:
            raise BatchError('--min is longer than --max')
        s = run(links, opts)
        return 1 if s['failed'] else 0
    except BatchError as e:
        print('Error:', e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
