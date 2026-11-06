"""Update check against the app's GitHub releases. No GUI code, only the standard library.

check() asks GitHub for the latest release at most once a day and remembers the answer;
install() downloads that release's source archive and runs its install.sh, which copies
the app over the installed one. Settings stay where they are, and the new version runs
from the next start. A copy running from a git checkout never installs anything.
whats_new() gives the release notes to show once on the first start after an update.
"""

import html
import json
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

CHECK_EVERY = 24 * 3600  # seconds between automatic checks
TIMEOUT = 10


def parse_version(text):
    """"v1.4.0" or "1.4" -> (1, 4, 0); None if it isn't a plain version number."""
    parts = text.strip().lstrip("vV").split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    return tuple(nums + [0] * (3 - len(nums)))


class Updater:
    def __init__(self, repo, version, app_dir, state_file):
        self.repo = repo                    # "Owner/name" on GitHub
        self.version = version              # the running version, "1.4.0"
        self.app_dir = Path(app_dir)
        # {"auto": bool, "checked": time, "latest": {...}, "installed": tag, "failed": tag,
        #  "seen": the version whose notes were shown last}
        self.state_file = Path(state_file)
        self.state = self._load()
        self._lock = threading.Lock()  # check() and whats_new() run at the same time on start

    def _load(self):
        try:
            state = json.loads(self.state_file.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        with self._lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2) + "\n")
            tmp.replace(self.state_file)

    @property
    def auto(self):
        return bool(self.state.get("auto"))

    @auto.setter
    def auto(self, value):
        self.state["auto"] = bool(value)
        self._save()

    @property
    def dev_checkout(self):
        """Running from a git clone: installing over it would clobber local work."""
        return (self.app_dir / ".git").exists()

    def installed(self, latest):
        """Already installed by an earlier update: waiting for a restart."""
        return self.state.get("installed") == latest["tag"]

    def failed(self, latest):
        """Its install failed before. Auto mode doesn't retry it on every start."""
        return self.state.get("failed") == latest["tag"]

    def newer(self, latest):
        mine, theirs = parse_version(self.version), parse_version(latest["tag"])
        return bool(mine and theirs and theirs > mine)

    def check(self, force=False):
        """The latest release as {"tag", "url", "notes"} if it is newer than this version,
        else None. Asks GitHub only when forced or a day after the last check. Raises
        OSError/ValueError when GitHub can't be reached, so callers can stay quiet."""
        latest = self.state.get("latest")
        if force or not latest or time.time() - self.state.get("checked", 0) > CHECK_EVERY:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": self.repo.split("/")[-1]})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
            latest = {"tag": data["tag_name"], "url": data.get("html_url", ""),
                      "notes": data.get("body") or ""}
            self.state.update(latest=latest, checked=time.time())
            self._save()
        return latest if self.newer(latest) else None

    def whats_new(self):
        """The releases this start is the first one of, as [{"tag", "url", "notes"}], newest
        first; [] when there is nothing to show. A fresh install shows nothing, nor does a
        downgrade. Skipped versions show too (1.4 -> 1.6 also shows 1.5). Asks GitHub every
        time it has something to show, because notes are sometimes written after the
        release. Raises OSError/ValueError when GitHub can't be reached: try again on the
        next start. Call mark_seen() once the notes are on screen."""
        mine = parse_version(self.version)
        if not mine:
            return []
        seen = parse_version(self.state.get("seen") or "")
        if seen is None:
            installed = parse_version(self.state.get("installed") or "")
            if installed != mine:  # a fresh install, or an update by hand from before this
                self.mark_seen()
                return []
            seen = (0, 0, 0)  # updated in the app from a version that didn't track this
            only_mine = True
        else:
            only_mine = False
        if mine <= seen:
            if mine < seen:  # downgraded: show the next update's notes again
                self.mark_seen()
            return []
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/releases?per_page=30",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": self.repo.split("/")[-1]})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
        out = []
        for rel in data:
            v = parse_version(rel.get("tag_name") or "")
            if not v or rel.get("draft") or rel.get("prerelease"):
                continue
            notes = clean_notes(rel.get("body") or "")
            if notes and (v == mine or (seen < v < mine and not only_mine)):
                out.append((v, {"tag": rel["tag_name"], "url": rel.get("html_url", ""),
                                "notes": notes}))
        if not out:  # no notes written for these versions
            self.mark_seen()
        out.sort(key=lambda x: x[0], reverse=True)
        return [r for _v, r in out]

    def mark_seen(self):
        self.state["seen"] = self.version
        self._save()

    def install(self, latest):
        """Downloads the release and runs its install.sh. Raises RuntimeError with a
        message for the user when that fails."""
        try:
            self._install(latest)
        except (OSError, RuntimeError):
            self.state["failed"] = latest["tag"]
            self._save()
            raise
        self.state["installed"] = latest["tag"]
        self.state.pop("failed", None)
        self._save()

    def _install(self, latest):
        if self.dev_checkout:
            raise RuntimeError(f"Running from a git checkout in {self.app_dir}. Use git pull")
        url = f"https://github.com/{self.repo}/archive/refs/tags/{latest['tag']}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "release.tar.gz"
            req = urllib.request.Request(url, headers={"User-Agent": self.repo.split("/")[-1]})
            with urllib.request.urlopen(req, timeout=60) as r:
                archive.write_bytes(r.read())
            src = Path(tmp) / "src"
            src.mkdir()
            _extract(archive, src)
            tops = list(src.iterdir())
            if len(tops) != 1 or not (tops[0] / "install.sh").is_file():
                raise RuntimeError("The release download has no install.sh")
            # no terminal (new session, no stdin): an installer that needs sudo for new
            # system packages fails instead of waiting for a password nobody can type
            run = subprocess.run(["sh", str(tops[0] / "install.sh")], cwd=tops[0],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 start_new_session=True, check=False)
            if run.returncode != 0:
                last = (run.stderr.strip() or run.stdout.strip()).splitlines()[-1:]
                raise RuntimeError("The installer stopped" + (f": {last[0]}" if last else "")
                                   + ". Run install.sh in a terminal")


def clean_notes(text):
    """Release notes without GitHub's added "Full Changelog" line."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n")
             if not ln.strip().startswith("**Full Changelog**")]
    return "\n".join(lines).strip()


def notes_markup(text):
    """Release notes (GitHub Markdown) as Pango markup: headings and **bold** in bold,
    `code` in monospace, list items as bullets, links as their text."""
    out = []
    for ln in text.split("\n"):
        ln = ln.rstrip()
        head = re.match(r"#+\s+(.*)", ln)
        item = re.match(r"(\s*)[-*+]\s+(.*)", ln)
        if head:
            ln = f"**{head.group(1)}**"
        elif item:
            ln = f"{item.group(1)}• {item.group(2)}"
        ln = html.escape(ln, quote=False)
        ln = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", ln)
        ln = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", ln)
        ln = re.sub(r"`([^`]+)`", r"<tt>\1</tt>", ln)
        out.append(ln)
    return "\n".join(out)


def _extract(archive, dest):
    try:
        _extract_tar(archive, dest)
    except tarfile.TarError as e:  # a cut-off download, or a file the filter refuses
        raise RuntimeError(f"Couldn't unpack the release: {str(e).splitlines()[0]}") from e


def _extract_tar(archive, dest):
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:
            # Python without extraction filters (e.g. Debian 12's 3.11.2): only plain
            # files and folders, with no absolute or ../ paths
            for m in tar.getmembers():
                if not (m.isfile() or m.isdir()) or m.name.startswith("/") \
                        or ".." in m.name.split("/"):
                    raise RuntimeError(f"Unexpected file in the release archive: {m.name}")
            tar.extractall(dest)
