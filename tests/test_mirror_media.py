"""File routing uses media signatures and preserves outbound metadata protections."""
import base64
import struct
import zlib

import pytest

from ctb import cli, mirror as M
from ctb.config import Config
from ctb.topics import TopicMap
from conftest import GOOD_TOKEN
from test_local_bot_api import RecordingBot, local_cfg, mirror_for, sparse, MIB


def box(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


# Encoded short, silent media samples; decoded in each test without external tools.
SAMPLES = {
    'mp3': 'eJzzdDFmYQABpZDgYFcgzcfAwOyTWJZmZqxnqGdoYMSAAP9/OxxgQAeeeWn5QIofiDkZGFiSGBwRIBIBChCgAwEWIMAOBLiAAC8Q4D8CgOwFujEZ7EZkx6gwn4EwWJL+HU3cCHW1wBEG5gMMjEuAHAUGBpMGoLSPo6+riZ5BKMUAYdD/30JHNJvR7KHcAmwWCRwJpplFqB6qRQ84qtmDBEAeWk4Hi0AeukC7GEK2SODINTp56Opw8hAAGYF6Ow==',
    'm4a': 'eJyVUs1Kw0AQnm1af0CkoEWRYCPWm5a2liJ4iTcVPYmC4iXtJm10twnZbUFPPfoqPobgY3j1Oerspj9bKwWHDDOzM/vNfLMBADuQL/F1/cwByICyoYg4ag3A2uVR1AcAxvsdCjNSdLUSUN9UyGzV79iFhZLB70Im3jP6j/JZ97Tm0YomDCH/71vyqRRobZ8JOb2hcLOju+SD09BDx+H0L+777u2nDo46lCXjjIh6XbPyBmN67nUp81UNYTzsBujkBZ8BLdH03KaJHxgU1noJc1KfVIRsMvSfhBTUqHngcd2bo00gPx4VpeELqgiDNRgMDjCZRbvlFiZ0SJjDk1zh7e4bltAjGUVdSL0mJSpe0evRXSyFa+MkLaOz0tWRPcHcq8FQnW9ACrBIN4VsRVNM6xLtjmjHVIEmEWOjWchwqCdotmOYZswJvB6Vai/33Jfj/eyZj4UPnHhxzIwp4TBM/4jSu4wi/SCevqwXe+X1g8ZxuVquVmpqHUHi+2hPORZ9qWxrks1U3HWy7Swv0h/g/mrQ',
    'mp4': 'eJx9VM2PGzUUd3ahqhCHHpaPwx5M2QOHzezM5KNthEXRUpEDSFyoQEKMPLYnM4o99tpONukJJA78Bf0TkCoB155Ae4ADQtxZgYQQEgJVokd6gfA8yTbZArViv6/fe8/vPU8QQrjwc1M5rRDaQoHCTumUJcp0E4S2d5TWU4SQVNOSo3Nr+7dmt1D4rVfrPOpR+Tp67NqC3yve0jHw7/lxk3P739FC7sfn+c+8l1YboT3BvQO6K6Tza49V3Ma3dVfxigKDFX+09uvNfvurRmiXXNozy7TiYhN5E2Q9pDWXImBauarqApidqWqCbl5zjy9tu9yKYqOMpydW4iXfuux8LoH/0nnHNzCfh6H9TytC0ZfQEOjwDLHzBuD7nSiJkjjFsspnab+76fH8YgFnF1CHLY6eWvyMnhsB/fT0/ZPXwkieCMfFl4e332mhC+W9X769fPLXgwdNLkOdWd0i7J3c21WLn33pLLrzTfvRGhdainahKnZev44DtltL09bXax3Ta/w2XA3RCfdhbO8q0dCwXtgcEYzVUmPkZrn71fId7N3xWjcjoI1z00roVXHWKxAvFlbAjLe+UwAC+tmFJxeLOz/e+P2LX0+Hd2/vf49PX/zjfmgobmOmrcBJv4dtJ01TnHd6/bhHwTCMAHDw5ls3Xm938as3DwHJBQPDoTZzKQqP0zjutNM47YGy9N4MDg6Oj4+j8MK0pHWk7eggZIlKryRgtPGVrt0AM5pTRhIMr4h0MBe51GxMkkE8iDGtqZw7QeJZZxDPkqSDlSClmGE3yYG7go2bgyucmeUkiWJwggOraiZ4FiIm4JFZWo8ESfqYlVYrmoFrgr0VUlYOuKuzq5x5YNiRIjFcgfJbuhYkTfaTBBfU+cy4cWUCYhngyGS6KJzwpJ1iX1rwCIGk1mNagpCtdU5WTKwVMa5tk4NVivpwj6r2wkoKINDncmLpPGNaGepBZtAi+IOpaggBQEsDprBUCQfNyjMzB77iBGaVUU5NqCLP8oqGTPByRFPXsahGpc+B00bU2UgbsC6VBlzHYg6xSdqLV2wGn324OhO1YBNPujFukoeOWuFK8LYse1htsFtGmC2wgs9HhKpAIGknivFRqIXEUR9YE+I2lM5I/xowzgtDurgyMCN4DzBCiEWPYPxhmOiD8IWKjz9Czyz+/vOb+yef3Pvhpw//AZ50ZBM=',
}


def sample(kind):
    return zlib.decompress(base64.b64decode(SAMPLES[kind]))


def mp3(id3=True):
    data = sample('mp3')
    size = 10 + sum(b << shift for b, shift in zip(data[6:10], (21, 14, 7, 0)))
    return data if id3 else data[size:]


def mp4(version=0, audio=False, malformed=False):
    data = sample('m4a' if audio else 'mp4')
    # Replace the real movie header while retaining the encoded tracks and samples.
    pos = data.index(b'mvhd') - 4
    old_size = int.from_bytes(data[pos:pos + 4], 'big')
    if malformed:
        mvhd = b'\x01'
    elif version == 1:
        mvhd = b'\x01' + bytes(19) + struct.pack('>IQ', 1000, 12500) + bytes(80)
    else:
        mvhd = bytes(12) + struct.pack('>II', 1000, 12500) + bytes(80)
    replacement = box(b'mvhd', mvhd)
    moov = data.index(b'moov') - 4
    old_moov_size = int.from_bytes(data[moov:moov + 4], 'big')
    adjusted = old_moov_size + len(replacement) - old_size
    return (data[:moov] + struct.pack('>I', adjusted) + data[moov + 4:pos] + replacement
            + data[pos + old_size:])


@pytest.mark.parametrize('suffix,data', [('.mp3', mp3()),
                                         ('.m4a', mp4(audio=True))])
def test_audio_upload_with_metadata_and_filtered_caption(tmp_path, suffix, data):
    path = tmp_path / ('example-track' + suffix)
    path.write_bytes(data)
    bot = RecordingBot()
    sender = mirror_for(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, redact_outgoing=True), tmp_path, bot)
    secret = 'sk-ant-api03-' + 'a' * 100
    sender.send_file('session', 's', str(path), 'caption ' + secret, 'title ' + secret, 'performer ' + secret)
    (sent,) = bot.methods('sendAudio')
    assert secret not in str(sent)
    assert sent['title'].startswith('title ') and sent['performer'].startswith('performer ')
    assert sent['caption'].startswith('📎 caption ')
    assert sent.get('duration') == (12 if suffix == '.m4a' else None)


def test_defaults_and_video(tmp_path):
    bot = RecordingBot()
    sender = mirror_for(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), tmp_path, bot)
    path = tmp_path / 'a_sample-track.m4a'
    path.write_bytes(mp4(1, audio=True))
    sender.send_file('session', 's', str(path))
    assert bot.methods('sendAudio')[0]['title'] == 'a sample track'
    assert bot.methods('sendAudio')[0]['duration'] == 12
    path = tmp_path / 'clip.mp4'
    path.write_bytes(mp4())
    sender.send_file('session', 's', str(path))
    assert bot.methods('sendVideo')[0]['supports_streaming'] == 'true'


@pytest.mark.parametrize('name,data', [('fake.mp3', b'plain text'), ('fake.m4a', b'not a container'),
                                       ('fake.mp4', b'not a video'), ('x.ogg', b'OggS')])
def test_unknown_or_forged_files_stay_documents(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    bot = RecordingBot()
    mirror_for(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), tmp_path, bot).send_file('t', 's', str(path))
    assert len(bot.methods('sendDocument')) == 1
    assert not bot.methods('sendAudio') and not bot.methods('sendVideo')


def test_local_large_audio_uses_staged_file(tmp_path):
    path = tmp_path / 'large.mp3'
    with path.open('wb') as f:
        f.write(mp3() + mp3(False) * 1000)
        f.truncate(51 * MIB)
    bot = RecordingBot()
    mirror_for(local_cfg(), tmp_path, bot).send_file('t', 's', str(path), audio_title='Title')
    assert bot.methods('sendAudio:path')[0]['title'] == 'Title'


def test_cli_arguments():
    args = cli._parser().parse_args(['mirror', '--file', 'x.mp3', '--audio-title', 'Track', '--performer', 'Artist'])
    assert (args.audio_title, args.performer) == ('Track', 'Artist')


def test_malformed_movie_header_skips_duration(tmp_path):
    path = tmp_path / 'short.m4a'
    path.write_bytes(mp4(audio=True, malformed=True))
    bot = RecordingBot()
    mirror_for(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), tmp_path, bot).send_file('t', 's', str(path))
    assert 'duration' not in bot.methods('sendAudio')[0]


def test_audio_options_reach_sender(monkeypatch):
    calls = []
    monkeypatch.setattr(M, 'build', lambda cfg: type('Sender', (), {
        'send_file': lambda self, *args: calls.append(args)})())
    assert M.run_file(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), 't', 's', 'x.mp3', 'cap', 'Track', 'Artist') == 0
    assert calls == [('t', 's', 'x.mp3', 'cap', 'Track', 'Artist')]


@pytest.mark.parametrize('name,data', [
    ('x.mp3', b'ID3\x04' + bytes(6)),
    ('x.mp3', bytes.fromhex('fffd9064') + bytes(900)),
    ('x.mp3', mp3()[:100]), ('x.mp3', mp3()[:-1]),
    ('x.mp4', mp4(audio=True)),
    ('x.m4a', mp4()),
    ('x.mp4', mp4()[:-1]),
])
def test_incomplete_or_mismatched_media_is_document(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    with path.open('rb') as stream:
        assert M._media_info(path.suffix, stream)[0] == 'sendDocument'


def test_streamed_brands_with_a_real_track_are_media(tmp_path):
    for name, data, method in (('x.m4a', mp4(audio=True).replace(b'M4A ', b'dash'), 'sendAudio'),
                               ('x.mp4', mp4().replace(b'avc1', b'iso6').replace(b'mp41', b'iso6'), 'sendVideo')):
        path = tmp_path / name
        path.write_bytes(data)
        with path.open('rb') as stream:
            assert M._media_info(path.suffix, stream)[0] == method


@pytest.mark.parametrize('local_path', [False, True])
@pytest.mark.parametrize('audio', [False, True])
def test_definite_rejection_retries_as_document(tmp_path, local_path, audio):
    class RejectMedia(RecordingBot):
        def upload(self, method, fields, field, filename, payload, timeout=120):
            super().upload(method, fields, field, filename, payload, timeout)
            if method in ('sendAudio', 'sendVideo'):
                raise M.TelegramError('Bad Request: unsupported media', 400, api_rejected=True)

        def upload_path(self, method, fields, field, path, timeout=1800):
            super().upload_path(method, fields, field, path, timeout)
            if method in ('sendAudio', 'sendVideo'):
                raise M.TelegramError('Bad Request: unsupported media', 400, api_rejected=True)

    name = 'sample.mp3' if audio else 'sample.mp4'
    path = tmp_path / name
    with path.open('wb') as f:
        f.write((mp3() + mp3(False) * 1000) if audio and local_path else (mp3() if audio else mp4()))
        if local_path:
            f.truncate(51 * MIB)
    bot = RejectMedia()
    cfg = local_cfg() if local_path else Config(bot_token=GOOD_TOKEN, owner_chat_id=7, redact_outgoing=True)
    sender = mirror_for(cfg, tmp_path, bot)
    secret = 'sk-ant-api03-' + 'a' * 100
    sender.send_file('t', 's', str(path), 'caption ' + secret, 'Track', 'Artist')
    media = bot.methods(('sendAudio' if audio else 'sendVideo') + (':path' if local_path else ''))[0]
    document = bot.methods('sendDocument' + (':path' if local_path else ''))[0]
    assert document['caption'] == media['caption'] and document['caption'].startswith('📎 caption ')
    assert secret not in document['caption']
    assert not {'title', 'performer', 'duration', 'supports_streaming'} & document.keys()
    if local_path:
        assert document['_path'] == media['_path'] and document['_field'] == 'document'
    else:
        assert document['_file'] == media['_file'] == name and document['_bytes'] == media['_bytes']


@pytest.mark.parametrize('local_path', [False, True])
@pytest.mark.parametrize('audio', [False, True])
@pytest.mark.parametrize('error', [M.TelegramError('timeout'), M.TelegramError('network error'),
                                    M.TelegramError('request rejected', 500), M.TelegramError('bad media', 400)])
def test_uncertain_upload_never_retries(tmp_path, local_path, audio, error):
    class Fail(RecordingBot):
        def upload(self, *args, **kwargs):
            raise error
        def upload_path(self, *args, **kwargs):
            raise error
    path = tmp_path / ('sample.mp3' if audio else 'sample.mp4')
    with path.open('wb') as f:
        f.write((mp3() + (mp3(False) * 1000 if local_path else b'')) if audio else mp4())
        if local_path:
            f.truncate(51 * MIB)
    bot = Fail()
    sender = mirror_for(local_cfg() if local_path else Config(bot_token=GOOD_TOKEN, owner_chat_id=7), tmp_path, bot)
    with pytest.raises(M.TelegramError):
        sender.send_file('t', 's', str(path))
    assert not bot.methods('sendDocument') and not bot.methods('sendDocument:path')


@pytest.mark.parametrize('local_path', [False, True])
def test_track_metadata_safe_for_local_guard(tmp_path, local_path):
    from ctb.telegram import reject_file_uris

    class Guarded(RecordingBot):
        def upload(self, method, fields, *args, **kwargs):
            reject_file_uris(fields)
            return super().upload(method, fields, *args, **kwargs)
        def upload_path(self, method, fields, *args, **kwargs):
            reject_file_uris(fields)
            return super().upload_path(method, fields, *args, **kwargs)

    path = tmp_path / 'sample.mp3'
    with path.open('wb') as f:
        f.write(mp3() + (mp3(False) * 1000 if local_path else b''))
        if local_path:
            f.truncate(51 * MIB)
    bot = Guarded()
    sender = mirror_for(local_cfg(), tmp_path, bot)
    sender.send_file('t', 's', str(path), audio_title='File:\x00 ' + 'x' * 1100,
                     performer=' file:\nartist')
    sent = bot.methods('sendAudio' + (':path' if local_path else ''))[0]
    assert sent['title'].startswith('file - ') and len(sent['title']) == 1000
    assert sent['performer'] == ' file -artist'


def test_media_sniff_reads_only_bounded_head():
    class HeadOnly:
        def seek(self, offset, whence=0):
            if whence == 2:
                self.pos = 2 * 1024 * MIB
            else:
                self.pos = offset
        def tell(self):
            return self.pos
        def read(self, n):
            assert n <= 1024 * 1024
            return (mp3(False) * (n // len(mp3(False)) + 1))[:n]
    assert M._media_info('.mp3', HeadOnly())[0] == 'sendAudio'


def test_api_rejection_marker_requires_telegram_error_body(monkeypatch):
    import io
    import json
    import urllib.error
    from ctb import telegram

    class Opener:
        def __init__(self, body):
            self.body = body
        def open(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, 'bad', {}, io.BytesIO(self.body))

    bot = telegram.Bot(GOOD_TOKEN)
    for body, expected in [(json.dumps({'ok': False, 'error_code': 400, 'description': 'bad media'}).encode(), True),
                           (b'not a telegram answer', False),
                           (json.dumps({'ok': False, 'error_code': 500}).encode(), False)]:
        monkeypatch.setattr(telegram, '_opener_for', lambda url: Opener(body))
        with pytest.raises(M.TelegramError) as exc:
            bot._request('https://example.invalid/sendAudio', b'', {}, 1)
        assert exc.value.api_rejected is expected
        assert M.Mirror._media_rejected('sendAudio', exc.value) is expected


def test_relay_preserves_only_explicit_rejection(monkeypatch):
    import json
    from ctb import creds

    class Socket:
        def __init__(self, *args):
            self.responses = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, value):
            pass
        def connect(self, path):
            pass
        def sendall(self, data):
            pass
        def shutdown(self, mode):
            pass
        def recv(self, size):
            if self.responses:
                return b''
            self.responses.append(True)
            return json.dumps({'error': 'bad media', 'code': 400, 'api_rejected': marker}).encode()

    monkeypatch.setattr(creds.socket, 'socket', Socket)
    for marker in (True, False):
        with pytest.raises(M.TelegramError) as exc:
            creds.relay_call({})
        assert M.Mirror._media_rejected('sendAudio', exc.value) is marker


def test_a_large_file_whose_tracks_are_out_of_reach_is_never_sent_as_video():
    class Big:
        def __init__(self): self.pos = 0
        def seek(self, pos, whence=0): self.pos = 5 * 1024 * 1024 if whence == 2 else pos
        def tell(self): return self.pos
        def read(self, n):
            head = (24).to_bytes(4, 'big') + b'ftypisom' + bytes(4) + b'isomiso6'
            return (head + (5 * 1024 * 1024 - 24).to_bytes(4, 'big') + b'mdat' + bytes(n))[:n]
    assert M._media_info('.mp4', Big())[0] == 'sendDocument'
    assert M._media_info('.m4a', Big())[0] == 'sendAudio'
