"""Record the README demo GIF: python make_gif.py OUT.gif

Builds a throwaway pipeline with setup.sh, runs each command below in it for
real, and animates the captured output in a terminal-style frame (needs
Pillow, and snakemake + snakeprune on PATH).
"""
import os, subprocess, sys, tempfile
from PIL import Image, ImageDraw, ImageFont

COMMANDS = [
    "snakeprune scan . results --summary",
    "snakeprune scan . results",
    "snakeprune scan . results --trash trash --yes",
    "snakemake -n",
]
COLS, ROWS, SIZE, PAD = 136, 24, 14, 14
BG, FG, DIM, PROMPT, CMD = "#0d1117", "#e6edf3", "#7d8590", "#3fb950", "#ffffff"
# snakeprune's stderr progress lines, drawn dimmer than the stdout results
PROGRESS = ("Loading ", "Loaded ", "Walking ", "Scanned ", "About to ", "moving to trash")
FONT = next(p for p in ("/System/Library/Fonts/Menlo.ttc",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf") if os.path.exists(p))

def run(cmd, cwd):
    out = subprocess.run(cmd, shell=True, cwd=cwd, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env={**os.environ, "COLUMNS": str(COLS)}).stdout
    lines = []
    for ln in out.rstrip("\n").splitlines():
        ln = ln.rstrip()
        if cmd.startswith("snakemake") and not ln.startswith("Nothing"):
            continue  # keep snakemake's one-line verdict, drop its banner
        while len(ln) > COLS:
            lines.append(ln[:COLS]); ln = ln[COLS:]
        lines.append(ln)
    return lines

def main(out_path):
    here = os.path.dirname(os.path.abspath(__file__))
    work = os.path.join(tempfile.mkdtemp(), "my-pipeline")
    subprocess.run(["bash", os.path.join(here, "setup.sh"), work], check=True)

    font = ImageFont.truetype(FONT, SIZE)
    cw = font.getbbox("M")[2]; lh = int(SIZE * 1.45)
    W, H = COLS * cw + 2 * PAD, ROWS * lh + 2 * PAD
    screen = []   # list of (text, colour) rows; a command row is ("$ cmd", None)
    frames, durations = [], []

    def snap(ms, cursor=False):
        img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
        for i, (text, colour) in enumerate(screen[-ROWS:]):
            y = PAD + i * lh
            if colour is None:  # prompt row
                d.text((PAD, y), "$", font=font, fill=PROMPT)
                d.text((PAD + 2 * cw, y), text[2:], font=font, fill=CMD)
            else:
                d.text((PAD, y), text, font=font, fill=colour)
        if cursor:
            n = min(len(screen), ROWS) - 1; last = screen[-1][0]
            x = PAD + len(last) * cw
            d.rectangle([x, PAD + n * lh + 2, x + cw - 2, PAD + n * lh + SIZE + 3], fill=FG)
        frames.append(img); durations.append(ms)

    for k, cmd in enumerate(COMMANDS):
        screen.append(("$ ", None)); snap(600, cursor=True)
        for i in range(0, len(cmd), 2):
            screen[-1] = ("$ " + cmd[: i + 2], None); snap(45, cursor=True)
        snap(400, cursor=True)
        for ln in run(cmd, work):
            screen.append((ln, DIM if ln.startswith(PROGRESS) else FG)); snap(70)
        durations[-1] = 3200 if k < len(COMMANDS) - 1 else 5000
        screen.append(("", FG))

    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=16) for f in frames]
    pal[0].save(out_path, save_all=True, append_images=pal[1:], duration=durations,
                loop=0, optimize=True, disposal=1)
    print(f"{out_path}: {len(frames)} frames, {W}x{H}")

if __name__ == "__main__":
    main(sys.argv[1])
