"""
Progress spinner tests.

The spinner exists because a run is mostly blocking network calls and looks
hung without it. It must never contaminate stdout (--json, pipes) and must
switch itself off when nobody is watching (pipes, launchd, CI).
"""
import io, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


class FakeTTY(io.StringIO):
    def __init__(self, tty=True): super().__init__(); self._tty = tty
    def isatty(self): return self._tty


# --- it draws when someone is watching -------------------------------------
out = FakeTTY(tty=True)
p = pb.Progress(stream=out)
chk("enabled on a tty", p.enabled)
p.start("evaluating…")
time.sleep(0.25)
p.stop()
drawn = out.getvalue()
chk("drew something", len(drawn) > 0)
chk("used a carriage return, not newlines", "\r" in drawn and "\n" not in drawn)
chk("showed the label", "evaluating…" in drawn)
chk("animated (more than one frame)",
    sum(drawn.count(f) for f in pb.Progress.FRAMES) > 1)
chk("cleared the line on stop", drawn.rstrip(" ").endswith("\r"))


# --- it stays silent when piped --------------------------------------------
out = FakeTTY(tty=False)
p = pb.Progress(stream=out)
chk("disabled when not a tty", not p.enabled)
p.start("evaluating…"); p.update("still going"); time.sleep(0.15); p.stop()
chk("wrote nothing when piped", out.getvalue() == "")

out = FakeTTY(tty=True)
p = pb.Progress(enabled=False, stream=out)
chk("--no-progress wins over a tty", not p.enabled)
p.start("x"); time.sleep(0.15); p.stop()
chk("wrote nothing when explicitly disabled", out.getvalue() == "")


# --- label changes, lifecycle, and not falling over ------------------------
out = FakeTTY(tty=True)
p = pb.Progress(stream=out)
p.start("first")
time.sleep(0.15)
p.update("second")
time.sleep(0.15)
p.stop()
v = out.getvalue()
chk("shows both labels in order", "first" in v and "second" in v
    and v.index("first") < v.index("second"))

out = FakeTTY(tty=True)
p = pb.Progress(stream=out)
p.update("started by update alone")
time.sleep(0.15)
p.stop()
chk("update() starts the spinner if needed", "started by update alone" in out.getvalue())

p = pb.Progress(stream=FakeTTY(tty=True))
p.start("a"); p.start("b")       # double start must not spawn two threads
time.sleep(0.12)
p.stop(); p.stop()               # double stop must not raise
chk("double start/stop is safe", p._thread is None)

p = pb.Progress(stream=FakeTTY(tty=True))
p.stop()
chk("stop before start is safe", p._thread is None)

with pb.Progress(stream=FakeTTY(tty=True)) as p:
    p.start("ctx")
chk("context manager stops the thread", p._thread is None)


# --- a longer label must not leave residue behind a shorter one ------------
out = FakeTTY(tty=True)
p = pb.Progress(stream=out)
p.start("a very long label indeed")
time.sleep(0.15)
p.update("short")
time.sleep(0.15)
p.stop()
frames = [f for f in out.getvalue().split("\r") if f.strip()]
chk("short label is padded over the long one",
    any(f.rstrip() .endswith("short") and len(f) >= len("a very long label indeed")
        for f in frames))


# --- a broken stream disables rather than crashes the run ------------------
class ExplodingStream:
    def isatty(self): return True
    def write(self, *a): raise OSError("stderr went away")
    def flush(self): pass


p = pb.Progress(stream=ExplodingStream())
p.start("boom")
time.sleep(0.2)
p.stop()
chk("broken stderr disables the spinner instead of raising", not p.enabled)

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
