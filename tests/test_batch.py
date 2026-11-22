"""batch.py against a small fake NeonClipping server (no downloads, no models)."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import batch


class Fake(BaseHTTPRequestHandler):
    jobs = {}
    made = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Fake.made.append(body)
        if 'bad' in body['url']:
            return self._send(400, {'detail': 'Choose a valid clip count and length range.'})
        job = 'job%d' % len(Fake.made)
        Fake.jobs[job] = {'polls': 0, 'fail': 'fail' in body['url']}
        self._send(200, {'job': job})

    def do_GET(self):
        if self.path.startswith('/make/'):
            j = Fake.jobs[self.path.split('/')[-1]]
            j['polls'] += 1
            if j['polls'] < 2:
                return self._send(200, {'status': 'running', 'stage': 'Downloading', 'progress': .3})
            if j['fail']:
                return self._send(200, {'status': 'error', 'error': 'This video is private.'})
            return self._send(200, {'status': 'done', 'stage': 'Clips ready', 'progress': 1, 'title': 'My: Stream/Part 1', 'source': {'url': 'x'},
                                    'clips': [{'file': 'abc123', 'title': 'Big win?', 'hook': 'WAIT FOR IT', 'score': 88, 'start': 10, 'end': 40, 'srt': True},
                                              {'file': 'def456', 'title': 'Second', 'hook': '', 'score': 70, 'start': 50, 'end': 80, 'srt': False},
                                              {'file': 'zzz', 'title': 'Broken', 'error': 'render failed', 'start': 1, 'end': 2}]})
        if self.path.startswith('/media/'):
            return self._send(200, b'MP4DATA', 'video/mp4')
        if self.path.startswith('/text/'):
            return self._send(200, b'1\n00:00:00,000 --> 00:00:01,000\nhi\n', 'text/plain')
        self._send(404, {'detail': 'nope'})


def opts(out, server, **kw):
    base = dict(out=out, server=server, count=4, min=30, max=60, mode='fast', layout='blur', style='gta', language='',
                resolution=1080, source_height=1080, poll=0, redo=False)
    base.update(kw)
    return SimpleNamespace(**base)


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        cls.url = 'http://127.0.0.1:%d' % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        Fake.jobs.clear()
        Fake.made.clear()

    def test_read_links_file_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'l.txt'
            f.write_text('# list\nhttps://a.com/1\n\nhttps://a.com/2\nhttps://a.com/1\n', encoding='utf-8')
            self.assertEqual(batch.read_links(['https://a.com/0'], f), ['https://a.com/0', 'https://a.com/1', 'https://a.com/2'])
            with self.assertRaises(batch.BatchError):
                batch.read_links(['not a link'])

    def test_safe_name(self):
        self.assertEqual(batch.safe_name('My: Stream/Part 1?'), 'My Stream Part 1')
        self.assertEqual(batch.safe_name('CON'), '_CON')
        self.assertEqual(batch.safe_name('...'), 'clip')
        self.assertLessEqual(len(batch.safe_name('x' * 200)), 60)

    def test_make_body_matches_app_fields(self):
        b = batch.make_body('https://y/1', opts('o', 's', language='hinglish', count=6))
        self.assertEqual(b['url'], 'https://y/1')
        self.assertEqual(b['count'], 6)
        self.assertEqual(b['language'], 'hinglish')
        self.assertNotIn('language', batch.make_body('https://y/1', opts('o', 's')))

    def test_full_run_saves_clips_csv_and_skips_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            o = opts(tmp, self.url)
            s = batch.run(['https://y/1', 'https://y/fail'], o, log=logs.append, sleep=lambda x: None)
            self.assertEqual(s, {'done': 1, 'skipped': 0, 'failed': 1, 'clips': 2})
            folder = Path(tmp) / '01 My Stream Part 1'
            self.assertEqual((folder / '01 Big win.mp4').read_bytes(), b'MP4DATA')
            self.assertTrue((folder / '01 Big win.srt').exists())
            self.assertTrue((folder / '02 Second.mp4').exists())
            self.assertFalse((folder / '02 Second.srt').exists())
            csv_text = (folder / 'clips.csv').read_text(encoding='utf-8-sig')
            self.assertIn('WAIT FOR IT', csv_text)
            self.assertIn('render failed', csv_text)
            data = json.loads((folder / 'clips.json').read_text(encoding='utf-8'))
            self.assertEqual(data['clips'][0]['seconds'], 30.0)
            manifest = json.loads((Path(tmp) / batch.MANIFEST).read_text(encoding='utf-8'))
            self.assertEqual(manifest['https://y/fail']['status'], 'failed')
            self.assertIn('private', manifest['https://y/fail']['error'])
            # second run: the finished link is skipped, the failed one is tried again
            s2 = batch.run(['https://y/1', 'https://y/fail'], o, log=logs.append, sleep=lambda x: None)
            self.assertEqual(s2['skipped'], 1)
            self.assertEqual(s2['failed'], 1)

    def test_server_error_detail_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            s = batch.run(['https://y/bad'], opts(tmp, self.url), log=logs.append, sleep=lambda x: None)
            self.assertEqual(s['failed'], 1)
            self.assertTrue(any('valid clip count' in l for l in logs))

    def test_app_not_running_stops_with_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(batch.BatchError) as e:
                batch.run(['https://y/1'], opts(tmp, 'http://127.0.0.1:9'), log=lambda m: None, sleep=lambda x: None)
            self.assertIn('Start the app first', str(e.exception))

    def test_main_validates_options(self):
        self.assertEqual(batch.main(['--count', '20', 'https://y/1']), 2)
        self.assertEqual(batch.main(['--min', '50', '--max', '20', 'https://y/1']), 2)
        self.assertEqual(batch.main([]), 2)


if __name__ == '__main__':
    unittest.main()
