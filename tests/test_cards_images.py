"""Private visual choices: real CLI, media capability, lifecycle and safe copies."""
import base64
import json
import os
import stat
import struct
from pathlib import Path
import time
import zlib

import pytest

from conftest import GOOD_TOKEN
from ctb import card_media as M, cards, cli, config, panel
from ctb.config import state_dir
from test_cards import only, run, sessions_are_live  # noqa: F401
from test_decisions import cfg, rig, roomy_budget, ORBIT_S, queued  # noqa: F401
from test_cockpit_apps_auth import request
from test_panel import OWNER, signed

def _decoder():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        import shutil
        return shutil.which("sips") is not None


# Stored copies always come from a decoder's clean re-encode (Pillow or macOS sips); without one every image is refused.
pytestmark = pytest.mark.skipif(not _decoder(), reason="needs Pillow or sips (CI installs Pillow)")


def png(width=24, height=12, metadata=True):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + (chunk(b'tEXt', b'Author\0private source metadata') if metadata else b'')
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\x60\x90\xc0' * width) * height)) + chunk(b'IEND', b''))


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'private-design-name.png'
    path.write_bytes(png())
    return path


def publish(rig, source):
    assert run(rig, 'choice', title='Which?', options=['First', 'Second'], images=[str(source)] * 2) == 0
    return only()


def mint(ident, init=None):
    status, _, body = request('POST', '/api/card-media-token', {panel.INIT_HEADER: signed() if init is None else init}, {'id': ident})
    assert status == 200
    return json.loads(body)


def get(ident, token=''):
    return request('GET', '/card-media/' + ident + ('?auth=' + token if token else ''))


def test_real_cli_copies_in_option_order_with_private_opaque_names(rig, source, monkeypatch, tmp_path):
    other = tmp_path / 'other.webp'  # format is sniffed, not inferred from the extension
    other.write_bytes(png(80, 40))
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', ORBIT_S)
    monkeypatch.setattr(config, 'load', lambda: rig.cfg)
    assert cli.main(['card', 'choice', 'Which?', '--option', 'First', '--option', 'Second',
                     '--image', str(source), '--image', str(other)]) == 0
    rec = only()
    assert len(set(rec['images'])) == 2
    raw = (state_dir() / cards.STATE_FILE).read_text()
    assert str(source) not in raw and source.name not in raw and str(other) not in raw
    assert cards.view(rec, time.time())['images'] == rec['images']
    root = state_dir() / 'card-media'
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for ident in rec['images']:
        assert M.ID.fullmatch(ident)
        assert stat.S_IMODE((root / ident).stat().st_mode) == 0o600
        data, mime = M.read(ident)
        assert mime in M.TYPES.values() and b'private source metadata' not in data
        assert len(data) <= M.MAX_STORED
    assert source.read_bytes() == png()  # source stays untouched
    assert M.read(rec['images'][0])[0] != M.read(rec['images'][1])[0]


@pytest.mark.parametrize('paths', [[], ['one'], ['one', 'two', 'three']])
def test_bad_image_count_refuses_before_reading_files(rig, paths, source):
    if not paths:  # no images keeps the original card schema
        assert run(rig, 'choice', title='Q', options=['A', 'B']) == 0
        assert 'images' not in only()
    else:
        assert run(rig, 'choice', title='Q', options=['A', 'B'], images=paths) == 1
        assert cards.open_cards() == []
        assert not (state_dir() / 'card-media').exists()


@pytest.mark.parametrize('bad', ['missing', 'text', 'svg', 'empty', 'large', 'dir', 'truncated', 'pixels', 'fifo'])
def test_invalid_files_are_refused_without_source_names_or_leaked_copies(rig, source, tmp_path, capsys, bad):
    path = tmp_path / 'sensitive-source'
    if bad == 'dir':
        path.mkdir()
    elif bad == 'fifo':
        os.mkfifo(path)
    elif bad == 'large':
        with path.open('wb') as f:
            f.truncate(M.MAX_SOURCE)
    elif bad != 'missing':
        path.write_bytes({'text': b'text', 'svg': b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'empty': b'',
                          'truncated': png()[:-7], 'pixels': png(1, 1).replace(struct.pack('>II', 1, 1), struct.pack('>II', 100000, 100000))}.get(bad, b''))
    assert run(rig, 'choice', title='Q', options=['A', 'B'], images=[str(source), str(path)]) == 1
    assert cards.open_cards() == []
    assert not list((state_dir() / 'card-media').iterdir())
    err = capsys.readouterr().err
    assert str(path) not in err and path.name not in err


def test_without_a_decoder_every_image_is_refused(source, monkeypatch):
    # Byte-level stripping cannot prove a clean copy (blocks between scans, ICC payloads): re-encode or refuse.
    monkeypatch.setattr(M, '_pillow', lambda data: None)
    monkeypatch.setattr(M.shutil, 'which', lambda name: None)
    with pytest.raises(M.MediaError, match='Pillow or sips'):
        M.prepare(source)


def test_decoder_downscales_to_1600(source):
    if M.shutil.which('sips') is None:
        pytest.importorskip('PIL')
    source.write_bytes(png(3200, 800))
    data = M.prepare(source)
    assert M.raster(data)[1] == 'jpeg'
    # JPEG SOF frame has height/width; no extra dependency needed for this assertion.
    p = 2
    while data[p+1] not in (0xC0, 0xC1, 0xC2):
        p += 2 + int.from_bytes(data[p+2:p+4], 'big')
    assert struct.unpack('>HH', data[p+5:p+9]) == (400, 1600)


@pytest.mark.parametrize('kind', ['png', 'jpeg', 'webp'])
def test_all_three_raster_formats_are_accepted_and_metadata_is_stripped(rig, source, monkeypatch, kind):
    if kind == 'jpeg':
        jpeg = M.prepare(source)
        if M.raster(jpeg)[1] != 'jpeg':
            pytest.skip('no JPEG encoder available')
        exif = b'Exif\0\0private source metadata'
        source.write_bytes(jpeg[:2] + b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + jpeg[2:])
    elif kind == 'webp':
        source.write_bytes(base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAw'
                                           'AQCdASoBAAEADsD+JaQAA3AAAAAA'))
    # The portable sanitizer itself, independent of a decoder's re-encode.
    clean, found = M.raster(source.read_bytes())
    assert found == kind and b'private source metadata' not in clean
    # The stored copy always comes from a decoder's re-encode (Pillow or sips); without one it is refused.
    if M._pillow(source.read_bytes()) is None and M.shutil.which('sips') is None:
        pytest.skip('no image decoder here')
    rec = publish(rig, source)
    for ident in rec['images']:
        data, mime = M.read(ident)
        assert b'private source metadata' not in data


def test_six_visual_options_work_and_seven_are_rejected_before_copy(rig, source):
    assert run(rig, 'choice', title='Six?', options=[str(n) for n in range(6)], images=[str(source)] * 6) == 0
    assert len(only()['images']) == 6
    assert run(rig, 'choice', title='Seven?', options=[str(n) for n in range(7)], images=[str(source)] * 7) == 1
    assert len(list((state_dir() / 'card-media').iterdir())) == 6


def test_refused_update_or_full_card_stack_rolls_back_prepared_copies(rig, source):
    rec = publish(rig, source)
    originals = set(rec['images'])
    assert run(rig, 'choice', title='Edit?', ident=rec['id'], options=['A', 'B'], images=[str(source)] * 2) == 1
    assert {p.name for p in (state_dir() / 'card-media').iterdir()} == originals
    for i in range(cards.MAX_OPEN_PER_SESSION - 1):
        assert run(rig, 'ask', title=f'Q{i}') == 0
    assert run(rig, 'choice', title='Overflow', options=['A', 'B'], images=[str(source)] * 2) == 1
    assert {p.name for p in (state_dir() / 'card-media').iterdir()} == originals


def test_media_requires_owner_minted_exact_image_capability(rig, source):
    rec = publish(rig, source)
    one, two = rec['images']
    scoped = mint(one)['token']
    assert get(one)[0] == 403
    for wrong in (signed(), panel.app_token(GOOD_TOKEN, OWNER, 'sample'), scoped[:-1] + ('0' if scoped[-1] != '0' else '1')):
        assert get(one, wrong)[0] == 403
    assert get(two, scoped)[0] == 403
    assert get(one, panel.card_media_token(GOOD_TOKEN, OWNER + 1, one, int(time.time()) + 600))[0] == 403
    assert get(one, panel.card_media_token('wrong', OWNER, one, int(time.time()) + 600))[0] == 403
    assert get(one, panel.card_media_token(GOOD_TOKEN, OWNER, one, int(time.time()) - 1))[0] == 403
    assert get(one, panel.card_media_token(GOOD_TOKEN, OWNER, one, int(time.time()) + 1200))[0] == 403
    code, headers, data = get(one, scoped)
    assert code == 200 and data == M.read(one)[0]
    assert headers['Content-Type'] == M.read(one)[1]
    assert headers['Cache-Control'] == 'private, no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert scoped.encode() not in data and str(source).encode() not in data
    assert request('GET', f'/card-media/{one}?auth={scoped}&auth={scoped}')[0] == 403
    for path in ('../cards.json', '%2e%2e%2fcards.json', one + '/more', one.upper(), one + '.jpg', 'a' * 100):
        assert get(path, scoped)[0] == 404
    missing = 'f' * 32
    assert get(missing, panel.card_media_token(GOOD_TOKEN, OWNER, missing, int(time.time()) + 600))[0] == 404
    for route in panel.ROUTES:
        assert request('POST', route, {panel.INIT_HEADER: scoped})[0] == 403
    assert request('GET', '/app/sample/?auth=' + scoped)[0] == 403


def test_mint_checks_owner_freshness_body_and_existing_image(rig, source):
    ident = publish(rig, source)['images'][0]
    for init in ('', signed(user_id=OWNER + 1), signed(tamper=True), signed(auth_date=int(time.time()) - 901)):
        assert request('POST', '/api/card-media-token', {panel.INIT_HEADER: init}, {'id': ident})[0] == 403
    auth_date = int(time.time()) - 840
    assert mint(ident, signed(auth_date=auth_date))['expires'] == auth_date + panel.DECIDE_MAX_AGE
    for body in ({}, {'id': '../secret'}, {'id': []}, {'id': ident, 'extra': True}):
        assert request('POST', '/api/card-media-token', {panel.INIT_HEADER: signed()}, body)[0] == 400
    assert request('POST', '/api/card-media-token', {panel.INIT_HEADER: signed()}, {'id': 'f' * 32})[0] == 404


@pytest.mark.parametrize('close', ['close', 'session', 'answer'])
def test_closed_answered_and_session_images_have_grace_then_are_deleted(rig, source, monkeypatch, close):
    now = [time.time()]
    monkeypatch.setattr(M.time, 'time', lambda: now[0])
    rec = publish(rig, source)
    now[0] += 2 * M.GRACE  # old copies still get a grace from closing, not creating
    if close == 'close':
        cards.close(ORBIT_S, rec['id'])
    elif close == 'session':
        cards.close_session(ORBIT_S)
    else:
        cards.answer(rig.cfg, rig.box, rec['id'], pick=1)
        assert 'option 2 of 2' in queued(rig, ORBIT_S)[0]['text']
    scoped = mint(rec['images'][0])['token']
    now[0] += M.GRACE - 1
    assert get(rec['images'][0], scoped)[0] == 200
    now[0] += 2
    cards.reconcile_progress(set())  # the real panel-state cleanup path
    assert all(M.read(i) is None for i in rec['images'])
    assert not cards.image_available(rec['images'][0])
    assert source.exists()


def test_symlink_copies_storage_and_nonregular_files_are_never_served(rig, source, tmp_path):
    rec = publish(rig, source)
    root = state_dir() / 'card-media'
    ident = rec['images'][0]
    scoped = mint(ident)['token']
    (root / ident).unlink()
    (root / ident).symlink_to(source)
    assert get(ident, scoped)[0] == 404
    (root / ident).unlink()
    os.mkfifo(root / ident)
    assert get(ident, scoped)[0] == 404
    (root / ident).unlink()
    root.rename(state_dir() / 'saved-card-media')
    root.symlink_to(tmp_path, target_is_directory=True)
    assert M.read(ident) is None
    with pytest.raises(M.MediaError):
        M.store_many([source])


def test_orphans_cleanup_and_failed_delivery_preserve_live_images(rig, source, monkeypatch):
    rec = publish(rig, source)
    orphan, = M.store_many([source])
    old = time.time() - M.GRACE - 1
    os.utime(state_dir() / 'card-media' / orphan, (old, old))
    cards.reconcile_progress(set())
    assert M.read(orphan) is None and all(M.read(i) for i in rec['images'])
    def broken(*a, **kw):
        raise OSError('full')
    monkeypatch.setattr(rig.box, 'put', broken)
    with pytest.raises(OSError):
        cards.answer(rig.cfg, rig.box, rec['id'], pick=0)
    assert only()['state'] == 'open' and all(cards.image(i) for i in rec['images'])


def test_wire_state_keeps_image_ids_without_translating_or_redacting_them(rig, source):
    rec = publish(rig, source)
    class Hebrew:
        def he(self, text):
            return 'תרגום: ' + text if text else ''
    out = panel.build_state(rig.cfg, tr=Hebrew(), light=True)
    row, = out['cards']
    assert row['images'] == rec['images']
    assert 'images_he' not in row and row['options_he'] == ['תרגום: First', 'תרגום: Second']
    assert str(source) not in json.dumps(out)


def _jpeg_with(blocks):
    """A minimal JPEG byte stream: SOI, the given (marker, body) blocks, a SOF0, an empty SOS and EOI."""
    seg = lambda m, body: bytes([0xFF, m]) + (len(body) + 2).to_bytes(2, 'big') + body
    sof = seg(0xC0, b'\x08' + struct.pack('>HH', 10, 20) + b'\x01\x01\x11\x00')
    return b'\xff\xd8' + b''.join(seg(m, b) for m, b in blocks) + sof + seg(0xDA, b'\x01\x01\x00\x00\x3f\x00') + b'\x00\xff\xd9'


def _exif(orientation, order='MM'):
    o = 'big' if order == 'MM' else 'little'
    tiff = order.encode() + (42).to_bytes(2, o) + (8).to_bytes(4, o) + (1).to_bytes(2, o)
    tiff += (0x0112).to_bytes(2, o) + (3).to_bytes(2, o) + (1).to_bytes(4, o) + orientation.to_bytes(2, o) + b'\0\0'
    return b'Exif\0\0' + tiff + (0).to_bytes(4, o)


def test_jpeg_copy_keeps_only_decoding_blocks():
    data = _jpeg_with([(0xE0, b'JFIF\0\x01\x01\0\0\x01\0\x01\0\0'), (0xE1, _exif(6)), (0xE2, b'ICC_PROFILE\0x'),
                       (0xE5, b'maker private serial 12345'), (0xEC, b'Ducky private'), (0xEE, b'Adobe\0'),
                       (0xFE, b'comment private')])
    out, kind = M.raster(data)
    assert kind == 'jpeg'
    assert b'private' not in out and b'Exif' not in out
    assert b'JFIF' in out and b'ICC_PROFILE' in out and b'Adobe' in out


@pytest.mark.parametrize('order', ['MM', 'II'])
@pytest.mark.parametrize('value', range(1, 9))
def test_exif_orientation_is_read_before_stripping(order, value):
    assert M.exif_orientation(_jpeg_with([(0xE1, _exif(value, order))])) == value
    assert M.exif_orientation(b'not a jpeg') is None
    assert M.exif_orientation(_jpeg_with([(0xE1, b'Exif\0\0junk')])) is None


def test_sips_path_turns_phone_photos_upright(source, monkeypatch):
    source.write_bytes(_jpeg_with([(0xE1, _exif(6))]))
    monkeypatch.setattr(M, '_pillow', lambda data: None)
    monkeypatch.setattr(M.shutil, 'which', lambda name: '/usr/bin/sips')
    seen = []
    def run(argv, **kw):
        seen.append(argv)
        out = argv[argv.index('--out') + 1]
        Path(out).write_bytes(_jpeg_with([]))
        return type('P', (), {'returncode': 0})()
    monkeypatch.setattr(M.subprocess, 'run', run)
    M.prepare(source)
    assert seen and seen[0][seen[0].index('-r') + 1] == '90'


def test_without_a_decoder_a_turned_or_large_image_is_refused(source, monkeypatch):
    monkeypatch.setattr(M, '_pillow', lambda data: None)
    monkeypatch.setattr(M.shutil, 'which', lambda name: None)
    source.write_bytes(_jpeg_with([(0xE1, _exif(6))]))
    with pytest.raises(M.MediaError, match='Pillow or sips'):
        M.prepare(source)
    source.write_bytes(png(3200, 800))
    with pytest.raises(M.MediaError, match='Pillow or sips'):
        M.prepare(source)
