"""A small stand-in for the CLI's multi-select question, drawn exactly like the real screen captured from
Claude Code 2.1.314 (tests/fixtures/cli2.1.314/multi*.txt) and moving like it does: Down/Up walk the rows
1, 2, 3, 4, Submit, 5; Space ticks a checkbox row; Enter on a checkbox row toggles it (it does not submit);
Enter on Submit finishes. Used in-process and, as a script, inside a real tmux pane."""
import json
import sys

ORDER = ["1", "2", "3", "4", "S", "5"]
NAMES = {"1": "Cheese", "2": "Olives", "3": "Mushrooms", "4": "Type something"}
SUB = {"1": "Mozzarella", "2": "Black", "3": "Fresh"}
RULE = "─" * 100


class Sim:
    def __init__(self, ticks=(False, False, False, False), cur=0, last=True):
        self.ticks, self.cur, self.done, self.last = list(ticks), cur, None, last

    def render(self) -> str:
        if self.done is not None:  # the dialog is gone, the idle prompt is back
            return "⏺ All done.\n\n" + RULE + "\n❯ \n" + RULE + "\n  ⏸ manual mode on · ? for shortcuts"
        rows = ["❯ please go", RULE, "←  ☐ Toppings  ✔ Submit  →" if self.last else "←  ☐ Toppings  ☐ Database  ✔ Submit  →", "Which toppings do you want?"]
        for i, k in enumerate("1234"):
            mark = "[✔]" if self.ticks[i] else "[ ]"
            lead = "❯" if ORDER[self.cur] == k else " "
            rows.append(f"{lead} {k}. {mark} {NAMES[k]}")
            if k in SUB:
                rows.append(f"         {SUB[k]}")
        rows.append(("❯" if ORDER[self.cur] == "S" else " ") + ("    Submit" if self.last else "    Next"))
        rows += [RULE, ("❯" if ORDER[self.cur] == "5" else " ") + " 5. Chat about this",
                 "Enter to select · ↑/↓ to navigate · ctrl+g to edit in VS Code · Esc to cancel"
                 if self.last else "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"]
        return "\n".join(rows)

    def press(self, key: str) -> None:
        if self.done is not None:
            return
        here = ORDER[self.cur]
        if key == "Down":
            self.cur = min(self.cur + 1, len(ORDER) - 1)
        elif key == "Up":
            self.cur = max(self.cur - 1, 0)
        elif key in ("Space", "Enter") and here in "1234":
            self.ticks["1234".index(here)] ^= True
        elif key == "Enter" and here == "S":
            self.done = [NAMES[k] for i, k in enumerate("1234") if self.ticks[i] and k != "4"]
        elif key == "Escape":
            self.done = []


class PreviewSim:
    """The side-box question of the real CLI: a digit only moves the selector, Enter answers."""

    def __init__(self, cur=0):
        self.cur, self.done = cur, None

    def render(self) -> str:
        if self.done is not None:
            return "⏺ All done.\n\n" + RULE + "\n❯ \n" + RULE + "\n  ⏸ manual mode on · ? for shortcuts"
        box = ["┌──────────────────────────────────────────┐", "│ +------+                                 │",
               "│ | A | B |                                │", "│ +------+                                 │",
               "└──────────────────────────────────────────┘", "Notes: press n to add notes"]
        rows = ["❯ please go", RULE, " ☐ Layout", "Which layout do you prefer?"]
        for i, name in enumerate(("Compact", "Roomy")):
            rows.append(f"{'❯' if self.cur == i else ' '} {i + 1}. {name}".ljust(34) + (box[0] if i == 0 else "│ +--------+") )
        rows += [" " * 34 + b for b in box[1:]]
        rows += [RULE, "  Chat about this", "Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel"]
        return "\n".join(rows)

    def press(self, key: str) -> None:
        if self.done is not None:
            return
        if key == "Down":
            self.cur = min(self.cur + 1, 1)
        elif key == "Up":
            self.cur = max(self.cur - 1, 0)
        elif key in ("1", "2"):
            self.cur = int(key) - 1  # a digit only moves the selector
        elif key == "Enter":
            self.done = ["Compact", "Roomy"][self.cur]


if __name__ == "__main__":  # python multisim.py <result file> <ticks e.g. 0100 | next | preview>
    import termios, tty
    result, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "0000")
    sim = PreviewSim() if mode == "preview" else Sim([c == "1" for c in mode] if mode != "next" else (False,) * 4,
                                                     last=mode != "next")
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    out = sys.stdout

    def draw():
        out.write("\x1b[2J\x1b[H" + sim.render().replace("\n", "\r\n") + "\r\n")
        out.flush()
    draw()
    buf = b""
    while sim.done is None:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            break
        buf += ch
        if buf in (b"\x1b", b"\x1b[", b"\x1bO"):
            continue
        key = {b"\x1b[B": "Down", b"\x1bOB": "Down", b"\x1b[A": "Up", b"\x1bOA": "Up", b" ": "Space", b"\r": "Enter", **{str(i).encode(): str(i) for i in (1, 2)},
               b"\n": "Enter"}.get(buf, "?")
        buf = b""
        sim.press(key)
        draw()
    open(result, "w").write(json.dumps(sim.done))
