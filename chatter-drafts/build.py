"""Build and lint the EDCoPilot custom chatter drafts.

Combines three sources into chatter-drafts/out/, one file per chatter type:
  1. the server's stock templates (src/edcopilot/templates.py), with the parts
     EDCoPilot's own files do not use removed;
  2. the lines already in the live custom files, so nothing the commander
     wrote is lost;
  3. the hand-written exchanges in chatter-drafts/personal/.

Nothing is written to the EDCoPilot folder. Run from the repo root:

    venv/Scripts/python.exe chatter-drafts/build.py [path to live custom files]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from src.edcopilot.templates import EDCoPilotTemplateManager  # noqa: E402

BLOCK_FILES = [
    "EDCoPilot.SpaceChatter.Custom.txt",
    "EDCoPilot.CrewChatter.Custom.txt",
    "EDCoPilot.DeepSpaceChatter.Custom.txt",
]
CHITCHAT_FILE = "EDCoPilot.ChitChat.Custom.txt"

# Every speaker and token below appears in EDCoPilot's own stock chatter files.
SPEAKERS = {
    "ship1", "ship2", "ship3", "ship4", "stationname",
    "EDCoPilot", "Operations", "Helm", "Engineering", "Number1", "Science",
}
TOKENS = {
    "stationname", "callsign", "othercallsign", "mycallsign", "flightnum", "myshipname",
    "localdestination", "starsystem", "randomstarsystem", "fuellevels", "shipCorporation",
    "cmdrname", "cmdraddress", "RandomShipName",
}
CHITCHAT_TOKENS = {"Commander", "fuellevels", "cmdraddress"}

# Template lines that use a token EDCoPilot does not know.
TEMPLATE_FIXES = {
    "We are approximately <DistanceFromSol> light years from Sol.": "We are a long way from Sol.",
    "Cargo manifest shows <CargoCount> of <CargoCapacity> tons loaded.":
        "Cargo manifest checked. Everything on it is actually in the hold.",
    "Credits updated: <Credits>. Profitable trading run.": "Payment received. Profitable trading run.",
    "[<Security>]": "[<Number1>]",
}


def clean_template(text: str) -> str:
    """Drop comments and the '(Condition)' suffix, which the stock files never use."""
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        line = re.sub(r"^\[example\]\s*\(.*\)\s*$", "[example]", line)
        for old, new in TEMPLATE_FIXES.items():
            line = line.replace(old, new)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def normalise_existing(text: str) -> str:
    """Put each '[<Speaker>]' of a hand-written block on its own line."""
    text = text.replace("\r", "")
    text = re.sub(r"\[example\][ \t]*(?=\[<)", "[example]\n", text)
    text = re.sub(r"(?<=\S)[ \t]*(?=\[<[A-Za-z0-9]+>\])", "\n", text)
    text = re.sub(r"(\[<[A-Za-z0-9]+>\]):", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def lint_blocks(name: str, text: str) -> list:
    problems = []
    inside = False
    for number, line in enumerate(text.splitlines(), 1):
        where = "%s:%d" % (name, number)
        if not line.strip():
            continue
        if not line.isascii():
            problems.append("%s non-ASCII character" % where)
        if line == "[example]":
            if inside:
                problems.append("%s block opened twice" % where)
            inside = True
            continue
        if line == "[\\example]":
            if not inside:
                problems.append("%s block closed without opening" % where)
            inside = False
            continue
        if not inside:
            problems.append("%s line outside a block: %s" % (where, line[:40]))
            continue
        match = re.match(r"^\[<([A-Za-z0-9]+)>\] (\S.*)$", line)
        if not match:
            problems.append("%s not a '[<Speaker>] text' line: %s" % (where, line[:40]))
            continue
        if match.group(1) not in SPEAKERS:
            problems.append("%s unknown speaker <%s>" % (where, match.group(1)))
        for token in re.findall(r"<([A-Za-z0-9_]+)>", match.group(2)):
            if token not in TOKENS:
                problems.append("%s unknown token <%s>" % (where, token))
    if inside:
        problems.append("%s last block is not closed" % name)
    return problems


def lint_chitchat(name: str, text: str) -> list:
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.isascii():
            problems.append("%s:%d non-ASCII character" % (name, number))
        for token in re.findall(r"<([A-Za-z0-9_]+)>", line):
            if token not in CHITCHAT_TOKENS:
                problems.append("%s:%d unknown token <%s>" % (name, number, token))
    return problems


def main() -> int:
    live = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/EDCoPilot/User custom files")
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    templates = EDCoPilotTemplateManager().generate_all_templates()
    problems = []
    for name in BLOCK_FILES:
        parts = [clean_template(templates[name])]
        if (live / name).exists():
            parts.append(normalise_existing((live / name).read_text(encoding="utf-8", errors="replace")))
        parts.append((ROOT / "personal" / name).read_text(encoding="utf-8").strip())
        text = "\n\n".join(part for part in parts if part) + "\n"
        problems += lint_blocks(name, text)
        (out / name).write_text(text, encoding="utf-8", newline="\r\n")
        print("%-40s %3d exchanges" % (name, text.count("[example]")))

    lines = []
    if (live / CHITCHAT_FILE).exists():
        existing = (live / CHITCHAT_FILE).read_text(encoding="utf-8", errors="replace")
        lines += [line.strip() for line in existing.splitlines() if line.strip()]
    personal = (ROOT / "personal" / CHITCHAT_FILE).read_text(encoding="utf-8")
    lines += [line.strip() for line in personal.splitlines() if line.strip()]
    text = "\n".join(lines) + "\n"
    problems += lint_chitchat(CHITCHAT_FILE, text)
    (out / CHITCHAT_FILE).write_text(text, encoding="utf-8", newline="\r\n")
    print("%-40s %3d lines" % (CHITCHAT_FILE, len(lines)))

    for problem in problems:
        print("LINT:", problem)
    print("lint problems:", len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
