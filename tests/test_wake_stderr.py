"""The async Stop hook (ctb hook-wake) must never write to stderr: Claude shows that stream as a
"Stop hook blocking error" (seen with macOS libmalloc's MallocStackLogging line)."""
import os
import subprocess
import sys
import textwrap


def test_hook_wake_silences_fd2_even_for_c_level_writes(tmp_path):
    script = textwrap.dedent("""
        import os, sys
        from ctb import wake
        wake.load_config = lambda: type("C", (), {"ready": False})()
        rc = wake.hook_wake("{}")
        os.write(2, b"MallocStackLogging: can't turn off malloc stack logging\\n")
        sys.exit(rc)
    """)
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60,
                         env={**os.environ, "HOME": str(tmp_path)})
    assert res.returncode == 0
    assert res.stderr == b""
