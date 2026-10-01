#!/usr/bin/env python3
"""Keep recordings (CSVs) and Bluetooth MAC addresses out of the Bio-dash repo.

    ./scrub_repo.py                 check: report CSVs + MACs in the files AND the full history
    ./scrub_repo.py fix-files       clean the current files only (then commit as usual)
    ./scrub_repo.py fix-history     rewrite ALL history: drop CSVs, replace MACs (needs git-filter-repo,
                                    then a force push) -- a backup bundle is written first
    ./scrub_repo.py install-hook    pre-commit hook that blocks commits adding CSVs or MACs

Options:  --yes (don't ask)   --no-ocr (skip reading screenshots)

MAC addresses are six hex pairs separated by ':' or '-'; each is replaced with
XX:XX:XX:XX:XX:XX (or the '-' form), keeping the separator. Bundled libraries (static/vendor/) and binary files
aren't edited. Screenshots can't be fixed automatically: if `tesseract` is installed, `check`
reads them and lists any that show an address so you can retake them.
Standard library only (git-filter-repo is needed just for fix-history).
"""
import datetime
import os
import re
import shutil
import subprocess
import sys

MAC = re.compile(rb"(?<![0-9A-Fa-f:\-])(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:\-])")
OCR_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:.\-]){5}[0-9A-Fa-f]{2}")   # OCR can misread a separator
PLACEHOLDER = {b":": b"XX:XX:XX:XX:XX:XX", b"-": b"XX-XX-XX-XX-XX-XX"}
SKIP_DIRS = ("/static/vendor/",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}
CMD = ARGS[0] if ARGS else "check"


def git(*args, text=True, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=text)
    if check and p.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {p.stderr.strip() if text else p.stderr.decode().strip()}")
    return p.stdout


def is_csv(path):
    return path.lower().endswith(".csv")


def editable(path, data):
    """Text files we may edit: not vendored, not images, no NUL bytes, valid UTF-8."""
    if any(d in "/" + path for d in SKIP_DIRS) or path.lower().endswith(IMAGE_EXT):
        return False
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def macs_in(data):
    return sorted({m.decode() for m in MAC.findall(data)})


def scrub(data):
    return MAC.sub(lambda m: PLACEHOLDER[b"-" if b"-" in m.group(0) else b":"], data)


def ocr_hits(data):
    if "--no-ocr" in FLAGS or not shutil.which("tesseract"):
        return None
    tmp = os.path.join(git("rev-parse", "--git-dir").strip(), "scrub_ocr_tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    out = subprocess.run(["tesseract", tmp, "-", "--psm", "11"], capture_output=True, text=True).stdout
    os.remove(tmp)
    return sorted({h for h in OCR_MAC.findall(out) if not h.upper().startswith("XX")})


def confirm(question):
    if "--yes" in FLAGS:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
def scan_files():
    """Tracked files in the working tree -> (csvs, {path: [macs]}, {image: [ocr hits]})"""
    csvs, macs, images = [], {}, {}
    for path in git("ls-files", "-z").split("\0"):
        if not path or not os.path.isfile(path):
            continue
        if is_csv(path):
            csvs.append(path); continue
        data = open(path, "rb").read()
        if path.lower().endswith(IMAGE_EXT):
            hits = ocr_hits(data)
            if hits:
                images[path] = hits
            continue
        if editable(path, data) and macs_in(data):
            macs[path] = macs_in(data)
    return csvs, macs, images


def scan_history():
    """Every blob in every commit -> (csv paths, {mac: example path}, {image path: hits})"""
    csv_paths, macs, images, seen_imgs = set(), {}, {}, set()
    for line in git("rev-list", "--objects", "--all").splitlines():
        sha, _, path = line.partition(" ")
        if not path:
            continue
        if is_csv(path):
            csv_paths.add(path); continue
        if git("cat-file", "-t", sha).strip() != "blob":
            continue
        data = subprocess.run(["git", "cat-file", "-p", sha], capture_output=True).stdout
        if path.lower().endswith(IMAGE_EXT):
            if sha not in seen_imgs:
                seen_imgs.add(sha)
                hits = ocr_hits(data)
                if hits:
                    images[path] = hits
            continue
        if editable(path, data):
            for m in macs_in(data):
                macs.setdefault(m, path)
    return csv_paths, macs, images


def report():
    csvs, file_macs, file_imgs = scan_files()
    h_csvs, h_macs, h_imgs = scan_history()
    print("== Current files ==")
    print(f"  CSV files tracked: {len(csvs)}")
    for p in csvs[:10]: print(f"    {p}")
    print(f"  Files containing MAC addresses: {len(file_macs)}")
    for p, ms in list(file_macs.items())[:15]: print(f"    {p}: {', '.join(ms)}")
    if len(file_macs) > 15: print(f"    … and {len(file_macs) - 15} more")
    print("== Full history (every commit, every branch and tag) ==")
    print(f"  CSV paths ever committed: {len(h_csvs)}")
    for p in sorted(h_csvs)[:10]: print(f"    {p}")
    print(f"  Distinct MAC addresses ever committed: {len(h_macs)}")
    for m, p in sorted(h_macs.items()): print(f"    {m}  (e.g. {p})")
    if "--no-ocr" in FLAGS or not shutil.which("tesseract"):
        print("  Screenshots: not checked (install tesseract-ocr to read them, or drop --no-ocr)")
    else:
        imgs = {**h_imgs, **file_imgs}
        print(f"  Screenshots showing an address: {len(imgs)}" + ("  <- retake these; they can't be fixed automatically" if imgs else ""))
        for p, hits in imgs.items(): print(f"    {p}: {', '.join(hits)}")
    clean_files = not csvs and not file_macs
    clean_hist = not h_csvs and not h_macs
    print()
    if clean_files and clean_hist:
        print("✓ Clean: no CSVs or MAC addresses in the files or the history.")
    else:
        if not clean_files: print("→ Current files: run  ./scrub_repo.py fix-files  and commit.")
        if not clean_hist: print("→ History:       run  ./scrub_repo.py fix-history  (rewrites history; force push afterwards).")
    return clean_files and clean_hist


def fix_files():
    csvs, file_macs, _ = scan_files()
    if not csvs and not file_macs:
        print("✓ Current files are already clean."); return
    print(f"Will stop tracking {len(csvs)} CSV file(s) (they stay on disk) and replace MACs in {len(file_macs)} file(s).")
    if not confirm("Continue?"):
        sys.exit("Aborted.")
    for p in csvs:
        git("rm", "--cached", "-q", "--", p)
    if csvs:
        # keep them from being re-added -- in the local-only exclude file, so no .gitignore is committed
        exclude = os.path.join(git("rev-parse", "--git-dir").strip(), "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        existing = open(exclude).read() if os.path.exists(exclude) else ""
        if "*.csv" not in existing.split():
            with open(exclude, "a") as f:
                f.write("\n# added by scrub_repo.py: recordings never go in the repo\n*.csv\n")
    for p in file_macs:
        data = open(p, "rb").read()
        with open(p, "wb") as f:
            f.write(scrub(data))
        git("add", "--", p)
    print("Done. Review with  git diff --cached  then commit, e.g.:")
    print('  git commit -m "Remove recordings and MAC addresses"')
    print("Older commits still contain them -- ./scrub_repo.py fix-history cleans those too.")


def fix_history():
    if subprocess.run(["git", "filter-repo", "--version"], capture_output=True).returncode != 0:
        sys.exit("fix-history needs git-filter-repo:\n  pip install git-filter-repo     (or: sudo apt install git-filter-repo)")
    if git("status", "--porcelain").strip():
        sys.exit("Commit or stash your changes first (the working tree must be clean).")
    h_csvs, h_macs, _ = scan_history()
    if not h_csvs and not h_macs:
        print("✓ History is already clean."); return
    print(f"Will rewrite ALL history: drop {len(h_csvs)} CSV path(s) and replace {len(h_macs)} MAC address(es):")
    for m in sorted(h_macs): print(f"    {m} -> {scrub(m.encode()).decode()}")
    print("Every commit id changes, so you'll need to force-push, and other clones must re-clone or reset.")
    if not confirm("Rewrite history?"):
        sys.exit("Aborted.")

    top = git("rev-parse", "--show-toplevel").strip()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = os.path.join(os.path.dirname(top), f"{os.path.basename(top)}-before-scrub-{stamp}.bundle")
    git("bundle", "create", bundle, "--all")
    print(f"Backup of the old history: {bundle}")

    remotes = {name: git("remote", "get-url", name).strip() for name in git("remote").split()}
    rules = os.path.join(git("rev-parse", "--git-dir").strip(), "scrub_replacements.txt")
    with open(rules, "w") as f:
        for m in sorted(h_macs):
            f.write(f"regex:(?i){re.escape(m)}==>{scrub(m.encode()).decode()}\n")
    cmd = ["git", "filter-repo", "--force", "--path-glob", "*.csv", "--invert-paths", "--replace-text", rules]
    p = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(rules)
    if p.returncode != 0:
        sys.exit(f"git filter-repo failed:\n{p.stderr}")
    for name, url in remotes.items():           # filter-repo removes remotes on purpose; put them back
        if name not in git("remote").split():
            git("remote", "add", name, url)
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "-q", "--prune=now")
    h_csvs2, h_macs2, _ = scan_history()
    print(f"Rewritten: {len(h_csvs2)} CSV paths and {len(h_macs2)} MAC addresses left in history.")
    if remotes:
        r = "origin" if "origin" in remotes else next(iter(remotes))
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        print(f"\nPublish it (replaces the history on {r}):\n  git push --force {r} {branch}\n  git push --force --tags {r}")
    print("Other clones: re-clone, or  git fetch && git reset --hard origin/<branch>.")


HOOK = """#!/bin/sh
# Installed by scrub_repo.py: block commits that add CSVs (recordings) or MAC addresses.
exec python3 "$(git rev-parse --show-toplevel)/scrub_repo.py" staged-check
"""


def staged_check():
    """pre-commit: look only at what's being committed."""
    problems = []
    for path in git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").split("\0"):
        if not path:
            continue
        if is_csv(path):
            problems.append(f"{path}: CSV file (recordings belong in Documents/Bio-dash, not the repo)")
            continue
        data = subprocess.run(["git", "show", f":{path}"], capture_output=True).stdout
        if editable(path, data) and macs_in(data):
            problems.append(f"{path}: MAC address {', '.join(macs_in(data))}")
    if problems:
        print("✗ Commit blocked by scrub_repo.py:", file=sys.stderr)
        for p in problems: print(f"   {p}", file=sys.stderr)
        print("Fix with  ./scrub_repo.py fix-files  (or commit with --no-verify if you really mean it).", file=sys.stderr)
        sys.exit(1)


def install_hook():
    hooks = os.path.join(git("rev-parse", "--git-dir").strip(), "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-commit")
    if os.path.exists(path) and "scrub_repo.py" not in open(path).read():
        sys.exit(f"A different pre-commit hook already exists at {path}; add this line to it:\n"
                 '  python3 "$(git rev-parse --show-toplevel)/scrub_repo.py" staged-check || exit 1')
    with open(path, "w") as f:
        f.write(HOOK)
    os.chmod(path, 0o755)
    print(f"Installed {path}: commits adding CSVs or MAC addresses will be refused.")


if __name__ == "__main__":
    if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode != 0:
        sys.exit("Run this inside the git repository.")
    os.chdir(git("rev-parse", "--show-toplevel").strip())
    actions = {"check": lambda: sys.exit(0 if report() else 1), "fix-files": fix_files,
               "fix-history": fix_history, "install-hook": install_hook, "staged-check": staged_check}
    if CMD not in actions:
        sys.exit(__doc__)
    try:
        actions[CMD]()
    except BrokenPipeError:          # output piped into head / less that closed early
        sys.exit(0)
