import json
import shutil
import subprocess
import threading
import time

import pytest

from ctb import mac_resources as mac, panel, panel_page
from ctb.config import Config
from conftest import GOOD_TOKEN
from test_panel_page import func

VM = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               10.
Pages active:                             100.
Pages inactive:                           20.
Pages speculative:                         5.
"""
DF = """Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/disk1 1000 600 400 60% /
"""
PS = """  80.0 1000 /usr/bin/pytest --token=secret-value
 20.0 9000 /usr/bin/ffmpeg -i /private/secret-value
 10.0 3000 /usr/bin/unknown --password=secret-value
  5.0 4000 /usr/bin/Chrome
"""


def test_parsers_and_safe_process_vocabulary():
    assert mac.parse_vm_stat(VM) == 35 * 4096
    assert mac.parse_vm_stat("bad") is None
    assert mac.parse_pressure("System-wide memory free percentage: 27%") == 27
    assert mac.parse_pressure("bad") is None
    assert mac.parse_df(DF) == {"free_bytes": 409600, "total_bytes": 1024000}
    assert mac.parse_df("bad") is None
    got = mac.parse_ps(PS)
    assert [p["kind"] for p in got["cpu"]] == ["test", "encoder", "other"]
    assert [p["kind"] for p in got["memory"]] == ["encoder", "browser", "other"]
    assert "secret-value" not in json.dumps(got)
    assert mac.process_kind("/private/secret-value") == "other"
    assert mac.parse_ps("invalid\nNaN 3 fake\n4 -1 fake") == {"cpu": [], "memory": []}


@pytest.mark.parametrize("value,expected", [(21, "ok"), (20, "warn"), (10, "critical"), (None, "unavailable")])
def test_memory_thresholds(value, expected):
    assert mac.low_status(value, mac.MEMORY_WARN_PERCENT, mac.MEMORY_CRITICAL_PERCENT) == expected


@pytest.mark.parametrize("value,expected", [(0.79, "ok"), (0.8, "warn"), (1.5, "critical"), (None, "unavailable")])
def test_cpu_thresholds(value, expected):
    assert mac.load_status(value) == expected


def test_disk_and_battery_thresholds():
    assert mac.low_status(15, mac.DISK_WARN_PERCENT, mac.DISK_CRITICAL_PERCENT) == "warn"
    assert mac.low_status(5, mac.DISK_WARN_PERCENT, mac.DISK_CRITICAL_PERCENT) == "critical"
    assert mac.parse_battery("Now drawing from 'Battery Power'\n -InternalBattery-0 10%; discharging")['status'] == "critical"
    assert mac.parse_battery("Now drawing from 'AC Power'\n -InternalBattery-0 10%; charging")['status'] == "ok"
    assert mac.parse_battery("Now drawing from 'AC Power'") == {"present": False}
    assert mac.parse_battery("") is None


def test_collect_and_state_do_not_expose_command_text(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    calls = []
    def run(*args):
        calls.append(args)
        return {"/usr/bin/vm_stat": VM, "/bin/df": DF, "/bin/ps": PS,
                "/usr/bin/pmset": "Now drawing from 'AC Power'",
                "/usr/bin/memory_pressure": "System-wide memory free percentage: 27%"}.get(args[0],
                    "1000000" if args[-1] == "hw.memsize" else "{ sec = 100, usec = 0 }")
    monkeypatch.setattr(mac, "_run", run)
    monkeypatch.setattr(mac.os, "getloadavg", lambda: (1, 2, 3))
    monkeypatch.setattr(mac.os, "cpu_count", lambda: 4)
    sample = mac.collect()
    assert ("/bin/ps", "-A", "-o", "%cpu=", "-o", "rss=", "-o", "args=") in calls
    assert sample["memory"]["available_bytes"] == 35 * 4096
    assert sample["memory"]["pressure_free_percent"] == 27
    assert sample["cpu"]["load"] == [1, 2, 3]
    assert sample["disk"]["free_bytes"] == 409600
    monkeypatch.setattr(mac, "snapshot", lambda: sample)
    state = panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="en"))
    assert state["mac"] == sample
    assert "secret-value" not in json.dumps(state)
    for lang in ("he", "en"):
        assert "secret-value" not in panel_page.render_page(lang, "nonce").decode()


def test_other_platform_is_unavailable(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "linux")
    assert mac.collect() == {"available": False, "status": "unavailable"}


def test_refresh_never_waits_for_collection(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        release.wait(2)
        return {"available": True, "status": "ok"}
    monkeypatch.setattr(mac, "collect", slow)
    monkeypatch.setattr(mac, "_snapshot", {"available": False, "status": "unavailable"})
    monkeypatch.setattr(mac, "_checked", 0.0)
    monkeypatch.setattr(mac, "_running", False)
    start = time.monotonic()
    assert not mac.snapshot()["available"]
    assert time.monotonic() - start < 0.5
    assert entered.wait(1)
    assert not mac.snapshot()["available"]
    release.set()
    for _ in range(100):
        if not mac._running:
            break
        time.sleep(.01)
    assert mac.snapshot()["available"]


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
@pytest.mark.parametrize("lang,title", [("he", "המחשב"), ("en", "This Mac")])
def test_card_renders_in_each_language(lang, title):
    strings = json.dumps(panel_page.STRINGS[lang], ensure_ascii=False)
    code = '''
    var S=STRINGS, nf=new Intl.NumberFormat("en");
    function el(tag,cls,text){return {children:[],textContent:text||"",appendChild:function(x){this.children.push(x);}};}
    FUNCTIONS
    var card=macCard({available:true,status:"warn",memory:{available_bytes:1024,status:"warn"},
      cpu:{load:[1,2,3],cores:4,status:"ok"},disk:{free_bytes:4096,status:"critical"},
      battery:{present:false},uptime_seconds:3600,processes:{cpu:[{kind:"test",cpu_percent:10}],memory:[]}});
    process.stdout.write(JSON.stringify(card));
    '''.replace('STRINGS', strings).replace('FUNCTIONS', func('macCard') + '\n' + func('rowInProcess'))
    result = subprocess.run([shutil.which("node"), "-e", code], capture_output=True, text=True, check=True)
    assert title in result.stdout
    assert panel_page.STRINGS[lang]["macMemoryWarn"] in result.stdout
    assert panel_page.STRINGS[lang]["macTest"] in result.stdout
    assert panel_page.STRINGS[lang]["macDiskCritical"] in result.stdout
    assert 'macCard(d.mac)' in func('renderAll')


def test_command_failure_and_timeout_are_unavailable(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == mac.COMMAND_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], mac.COMMAND_TIMEOUT)
    monkeypatch.setattr(mac.subprocess, "run", timeout)
    assert mac._run("/example") == ""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_fake_secret_never_reaches_rendered_card():
    sample = {"available": True, "status": "ok", "processes": mac.parse_ps(PS)}
    source = '''var S=STRINGS,nf=new Intl.NumberFormat("en");
    function el(t,c,x){return {children:[],textContent:x||"",appendChild:function(v){this.children.push(v)}}}
    FUNCTIONS
    process.stdout.write(JSON.stringify(macCard(SAMPLE)));'''
    source = source.replace('STRINGS', json.dumps(panel_page.STRINGS['en'])).replace(
        'FUNCTIONS', func('macCard') + '\n' + func('rowInProcess')).replace('SAMPLE', json.dumps(sample))
    result = subprocess.run([shutil.which("node"), "-e", source], text=True, capture_output=True, check=True)
    assert 'secret-value' not in result.stdout
    assert 'Test run' in result.stdout


def test_card_follows_owner_decisions_and_tasks():
    page = func('renderAll')
    assert page.index('macCard(d.mac)') > page.index('oldGroup(')
    assert page.index('macCard(d.mac)') > page.index('sec(S.openTasks')


@pytest.mark.parametrize('command,kind', [
    ('/usr/bin/python3 /opt/bin/pytest --sample=private', 'test'),
    ('/usr/local/bin/node /app/node_modules/vitest/vitest.mjs --sample=private', 'test'),
    ('/usr/bin/python3 /opt/bin/ffmpeg --sample=private', 'encoder'),
    ('/usr/bin/node /opt/bin/claude --sample=private', 'agent'),
    ('/usr/bin/xcodebuild build', 'build'),
    ('/private/pytest-cache/ordinary --sample=private', 'other'),
    ('/private/agy/ordinary --sample=private', 'other'),
    ('/usr/bin/ordinary /opt/bin/pytest', 'other'),
])
def test_realistic_process_names_and_directory_decoys(command, kind):
    result = mac.parse_ps('5.0 100 ' + command)
    assert result['cpu'][0]['kind'] == kind
    assert command not in json.dumps(result)
    assert 'private' not in json.dumps(result)


def test_light_state_does_not_sample(monkeypatch):
    monkeypatch.setattr(mac, 'snapshot', lambda: pytest.fail('light state started a sampler'))
    state = panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language='en'), light=True)
    assert 'mac' not in state


def test_refresh_releases_running_after_base_exception(monkeypatch):
    monkeypatch.setattr(mac, '_running', True)
    monkeypatch.setattr(mac, 'collect', lambda: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        mac._refresh()
    assert not mac._running


def test_unsupported_platform_never_starts_thread(monkeypatch):
    monkeypatch.setattr(mac.sys, 'platform', 'linux')
    monkeypatch.setattr(mac.threading, 'Thread', lambda **kw: pytest.fail('thread started'))
    assert mac.snapshot() == {'available': False, 'status': 'unavailable'}


def test_load_status_uses_five_minute_average(monkeypatch):
    monkeypatch.setattr(mac.sys, 'platform', 'darwin')
    monkeypatch.setattr(mac, '_run', lambda *args: '')
    monkeypatch.setattr(mac.os, 'getloadavg', lambda: (8, 1, 1))
    monkeypatch.setattr(mac.os, 'cpu_count', lambda: 4)
    assert mac.collect()['cpu']['status'] == 'ok'


def test_refresh_interval_and_honest_labels():
    assert 15 <= mac.CACHE_SECONDS <= 30
    assert 'lifetime' in panel_page.STRINGS['en']['macTopCpu'].lower()
    assert 'חיי' in panel_page.STRINGS['he']['macTopCpu']
    assert 'five' in panel_page.STRINGS['en']['macCpuWarn'].lower()
    assert 'חמש' in panel_page.STRINGS['he']['macCpuWarn']
    assert 'purgeable' in panel_page.STRINGS['en']['macDisk'].lower()
    assert 'ניתן לפינוי' in panel_page.STRINGS['he']['macDisk']
