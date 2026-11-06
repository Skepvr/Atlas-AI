#!/usr/bin/env python3
"""
Atlas: a private chat app for your local Ollama models.

Run:   python assistant.py      (Mac/Linux: python3 assistant.py)
Then:  it opens http://localhost:8765 in your browser.

Needs only Python 3 (no installs). Ollama must be running with at least one model.
Your chats are saved next to this file in assistant_data.json and never leave your computer.
"""
import base64, hmac, json, mimetypes, os, re, secrets, shlex, shutil, socket, struct, subprocess, sys, tempfile, time, threading, webbrowser, urllib.request, urllib.error, urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PORT = 8765
if getattr(sys, "frozen", False):  # packaged app: keep data in your home folder
    BASE = os.path.join(os.path.expanduser("~"), ".my-assistant")
    os.makedirs(BASE, exist_ok=True)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "assistant_data.json")
PING = {"t": time.time(), "seen": False}
CAPS = {}
ICONDIR = os.path.join(os.path.expanduser("~"), ".my-assistant", "icon")


def say(*a):
    if sys.stdout:  # no console when launched from a shortcut
        print(*a)
NO_OLLAMA = ("Can't reach Ollama. Make sure the Ollama app is running "
             "(or run 'ollama serve' in a terminal), then try again.")

TOKEN = secrets.token_hex(16)  # stops other websites from talking to this app
HOME = Path.home().resolve()
DOCS = DATA.replace("data.json", "docs.json")
UNDO = DATA.replace("data.json", "undo.json")
CATS = {
    "Images": "jpg jpeg png gif webp bmp svg heic tiff ico",
    "Videos": "mp4 mkv mov avi webm wmv flv",
    "Audio": "mp3 wav flac m4a ogg aac",
    "Documents": "pdf doc docx txt md rtf odt epub",
    "Spreadsheets": "xls xlsx csv ods",
    "Slides": "ppt pptx key odp",
    "Archives": "zip rar 7z tar gz bz2",
    "Installers": "exe msi dmg pkg deb appimage",
    "Code": "py js ts html css json java c cpp cs go rs sh",
}
EXT = {e: c for c, v in CATS.items() for e in v.split()}
SKIP = (".crdownload", ".part", ".tmp", ".download")


BLOCK = {"windows", "program files", "program files (x86)", "system", "usr", "etc", "bin", "lib", "var", "sys", "proc", "boot"}


def safe_folder(p):
    f = Path(os.path.expanduser(p)).resolve()
    if (f == HOME or len(f.parts) < 2 or f.parts[1].lower() in BLOCK or not f.is_dir()):
        raise ValueError("Pick a specific folder that exists, like ~/Videos or D:\\Videos\\Clips (not a whole drive, your home folder, or a system folder).")
    return f


OLD = "Review before deleting"
KEEP = set(CATS) | {"Other", "Folders", OLD}


def make_plan(folder, days=90):
    f, moves, now = safe_folder(folder), [], time.time()
    for x in sorted(f.iterdir()):
        n = x.name
        if n.startswith(".") or n in KEEP or n.lower().endswith(SKIP):
            continue
        if x.is_dir():
            moves.append({"src": str(x), "dst": str(f / "Folders" / n)})
            continue
        if not x.is_file():
            continue
        st = x.stat()
        age = (now - st.st_mtime) / 86400
        cat = EXT.get(x.suffix.lower().lstrip("."), "Other")
        why = None
        if st.st_size == 0:
            why = "empty file"
        elif cat == "Installers" and age > 14:
            why = "old installer"
        elif re.search(r" \(\d+\)\.[^.]+$", n) and age > 7:
            why = "looks like a duplicate download"
        elif age > days:
            why = f"untouched for {int(age)} days"
        if why:
            moves.append({"src": str(x), "dst": str(f / OLD / n), "why": why})
        else:
            moves.append({"src": str(x), "dst": str(f / cat / n)})
    return moves


def run_moves(folder, moves, mode="move"):
    f, done = safe_folder(folder), []
    for m in moves:
        a, d = Path(m["src"]).resolve(), Path(m["dst"]).resolve()
        if a.parent != f or not (a.is_file() or a.is_dir()) or d.parent.parent != f:
            continue  # only loose files directly inside the chosen folder
        d.parent.mkdir(exist_ok=True)
        n, i = d, 1
        while n.exists():  # never overwrite anything
            n = d.with_name(f"{d.stem} ({i}){d.suffix}")
            i += 1
        if mode == "link":  # same data, no extra disk space; the original stays where it is
            try:
                os.link(a, n)
            except OSError:
                shutil.copy2(a, n)
        elif mode == "copy":
            shutil.copy2(a, n)
        else:
            shutil.move(str(a), str(n))
        done.append({"src": str(a), "dst": str(n), "mode": mode})
    with open(UNDO, "w") as fh:
        json.dump(done, fh)
    return len(done)


def undo_moves():
    try:
        with open(UNDO) as fh:
            done = json.load(fh)
    except Exception:
        return 0
    n = 0
    for m in reversed(done):
        a, d = Path(m["src"]), Path(m["dst"])
        if m.get("mode") in ("link", "copy"):
            if a.exists() and d.exists():  # the original is still there, so remove only the extra copy
                d.unlink()
                n += 1
        elif d.exists() and not a.exists():
            shutil.move(str(d), str(a))
            n += 1
        if m.get("made", True):
            try:
                d.parent.rmdir()
            except OSError:
                pass
    os.remove(UNDO)
    return n


VID = (".mp4", ".mkv", ".webm", ".mov", ".avi")
IMG = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif")
MEDIA = VID + IMG
AUDIO_OUT = {"mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma"}
IMG_OUT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "ico", "avif"}


def list_videos(folder, deep=False):
    f = safe_folder(folder)
    items = []
    for root, dirs, names in os.walk(f):
        dirs[:] = [d for d in dirs if not d.startswith(".")] if deep else []
        for n in names:
            if n.lower().endswith(MEDIA):
                x = Path(root) / n
                st = x.stat()
                items.append((st.st_mtime, {"name": n, "path": str(x), "mb": round(st.st_size / 1048576)}))
    dirs = []
    for d in sorted(f.iterdir(), key=lambda x: x.name.lower()):
        if d.is_dir() and not d.name.startswith("."):
            try:
                cnt = sum(1 for x in d.iterdir() if x.suffix.lower() in MEDIA)
            except OSError:
                cnt = 0
            dirs.append({"name": d.name, "path": str(d), "count": cnt})
    try:  # a way back up, as long as that folder is one we are allowed to use
        safe_folder(str(f.parent))
        parent = str(f.parent)
    except ValueError:
        parent = ""
    return {"videos": [v for _, v in sorted(items, key=lambda t: -t[0])][:400], "folders": dirs, "parent": parent}


def rename_file(path, new):
    a = Path(path).resolve()
    if not a.is_file() or a.suffix.lower() not in MEDIA:
        raise ValueError("That isn't a video or image file.")
    base = (new or "").strip()
    if base.lower().endswith(a.suffix.lower()):
        base = base[:-len(a.suffix)]
    t = a.with_name(folder_name(base) + a.suffix)  # the extension is kept, so the file still opens
    if str(t) == str(a):
        return {"name": a.name, "path": str(a)}
    if t.exists() and os.path.normcase(str(t)) != os.path.normcase(str(a)):
        raise ValueError('There is already a file called "%s" here.' % t.name)
    try:
        a.rename(t)
    except PermissionError:
        raise ValueError("Windows says that file is in use. Close it in any other program and try again.")
    with open(UNDO, "w") as fh:
        json.dump([{"src": str(a), "dst": str(t), "mode": "move", "made": False}], fh)
    return {"name": t.name, "path": str(t)}


def folder_name(n):
    n = clean_name(n or "")[:80].strip(". ")
    if not n or n.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}:
        raise ValueError("Type a folder name first (letters and numbers work best).")
    return n


def folder_op(j):
    f = safe_folder(j["folder"])
    act = j.get("action")
    if act == "create":
        n = folder_name(j.get("name"))
        t = f / n
        if t.exists():
            raise ValueError('A folder named "%s" already exists here. Pick it from the list instead.' % n)
        t.mkdir()
        return {"name": n, "path": str(t)}
    t = (f / os.path.basename(str(j.get("name", "")))).resolve()
    if t.parent != f or not t.is_dir():
        raise ValueError("That folder isn't there any more.")
    if act == "rename":
        n = folder_name(j.get("new"))
        u = f / n
        if u.exists():
            raise ValueError('A folder named "%s" already exists here.' % n)
        t.rename(u)
        return {"name": n, "path": str(u)}
    if act == "delete":
        try:
            t.rmdir()  # only works when it is empty
        except OSError:
            raise ValueError("That folder isn't empty, so I left it alone. Move or delete what's inside first.")
        return {"ok": True}
    raise ValueError("Unknown folder action.")





def dest_dir(dest):
    d = Path(os.path.expanduser(dest)).resolve()
    if d == HOME or len(d.parts) < 2 or d.parts[1].lower() in BLOCK or not d.parent.exists():
        raise ValueError("Pick a destination like ~/Videos/Highlights. Its parent folder must exist, and it can't be a whole drive, your home folder or a system folder.")
    return d


def ffmpeg_path():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def convert_file(path, fmt, dest=""):
    ff = ffmpeg_path()
    if not ff:
        raise ValueError("Converting needs ffmpeg. Easiest: in a terminal run  pip install imageio-ffmpeg  then restart this app. Or install ffmpeg itself (Windows: winget install ffmpeg, Mac: brew install ffmpeg).")
    fmt = fmt.lower().lstrip(".")
    if not re.match(r"^[a-z0-9]{2,5}$", fmt):
        raise ValueError("Type a format like mp4, webp or mp3.")
    a = Path(path).resolve()
    if not a.is_file() or a.suffix.lower() not in MEDIA:
        raise ValueError("That isn't a video or image file.")
    out = dest_dir(dest.strip() or str(a.parent / "Converted"))
    out.mkdir(exist_ok=True)
    n, i = out / (a.stem + "." + fmt), 1
    while n.exists():  # never overwrite
        n = out / f"{a.stem} ({i}).{fmt}"
        i += 1
    still = a.suffix.lower() in IMG
    cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(a)]
    if fmt in AUDIO_OUT:
        cmd += ["-vn"]
    elif fmt == "gif" and not still:
        cmd += ["-vf", "fps=12,scale=480:-1:flags=lanczos"]
    elif fmt in IMG_OUT:
        if not still:
            cmd += ["-frames:v", "1"]  # one still picture from a video
        if fmt in ("jpg", "jpeg"):
            cmd += ["-q:v", "2"]
        if fmt == "ico":
            cmd += ["-vf", "scale='min(256,iw)':'min(256,ih)':force_original_aspect_ratio=decrease"]
    elif fmt in ("mp4", "m4v", "mov") and not still:
        cmd += ["-pix_fmt", "yuv420p"] + (["-movflags", "+faststart"] if fmt == "mp4" else [])
    cmd.append(str(n))
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        if n.exists():
            n.unlink()
        raise ValueError("ffmpeg couldn't make a .%s from that. %s" % (fmt, " ".join((r.stderr or "").strip().splitlines()[-1:])) + (" Does the file have any sound?" if fmt in AUDIO_OUT else ""))
    return {"out": str(n)}


def send_files(files, dest, mode="link"):
    d = dest_dir(dest)
    made = not d.exists()
    d.mkdir(exist_ok=True)
    done = []
    for x in files:
        a = Path(x).resolve()
        if not a.is_file() or a.suffix.lower() not in MEDIA or a.parent == d:
            continue
        n, i = d / a.name, 1
        while n.exists():  # never overwrite
            n = d / f"{a.stem} ({i}){a.suffix}"
            i += 1
        if mode == "link":  # same data, no extra space; falls back to a copy on another drive
            try:
                os.link(a, n)
            except OSError:
                shutil.copy2(a, n)
        elif mode == "copy":
            shutil.copy2(a, n)
        else:
            shutil.move(str(a), str(n))
        done.append({"src": str(a), "dst": str(n), "mode": mode, "made": made})
    with open(UNDO, "w") as fh:
        json.dump(done, fh)
    return {"n": len(done), "dest": str(d)}


GH_TOKEN = os.path.join(BASE, "github_token")
SECRET = re.compile(r"(^\.env|\.pem$|\.key$|\.pfx$|^id_rsa|^id_ed25519|secret|password|token|credential)", re.I)
SKIPDIR = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "outbox", "browser-profile"}
SKIPFILE = {"assistant_data.json", "assistant_docs.json", "assistant_undo.json", ".DS_Store", "Thumbs.db"}


def gh_token():
    try:
        with open(GH_TOKEN) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def gh_files(folder):
    f, ok, skip = safe_folder(folder), [], []
    for root, dirs, names in os.walk(f):
        dirs[:] = [d for d in dirs if d not in SKIPDIR]
        for n in names:
            p = Path(root) / n
            rel = p.relative_to(f).as_posix()
            if n in SKIPFILE or SECRET.search(n):
                skip.append([rel, "looks private"])
            elif p.stat().st_size > 50 * 1024 * 1024:
                skip.append([rel, "over 50 MB"])
            else:
                ok.append(rel)
    return f, ok[:300], skip, len(ok) > 300


class GHError(ValueError):
    def __init__(self, msg, code=0):
        super().__init__(msg)
        self.code = code


def gh_explain(code, path, msg):
    if "SAML" in msg or "SSO" in msg:
        return "This repository's organization requires single sign-on. Open your token's page on GitHub, click Configure SSO, and authorize the organization."
    if code == 401:
        return "GitHub doesn't accept this token. It may be mistyped, expired or deleted. Make a new one and paste it again."
    if path == "/user/repos" and code in (403, 404, 422):
        return "GitHub wouldn't create that repository. The name may already exist, or this token isn't allowed to create repositories. Create it on github.com (tick Add a README), untick the new-repository box here, or use a classic token with the repo box ticked."
    if "/git/" in path and code in (403, 404):
        return "The token can't write to this repository. Fine-grained token: set Contents to Read and write, and make sure this repo is selected under Repository access. Classic token: tick the repo box."
    if path.startswith("/repos/") and code == 404:
        return "Repository not found. Check the spelling (owner/name). With a fine-grained token, make sure this repo is selected under Repository access."
    if code == 403 and "rate limit" in msg.lower():
        return "GitHub says you've made too many requests. Wait a few minutes and try again."
    return "GitHub said %s: %s" % (code, msg)


def gh(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + gh_token(), "Accept": "application/vnd.github+json",
                 "User-Agent": "atlas", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except Exception:
            msg = ""
        raise GHError(gh_explain(e.code, path, msg), e.code)
    except urllib.error.URLError:
        raise GHError("Couldn't reach GitHub. Check your internet connection.")


def gh_check(repo):
    """Plain-language test of the saved token. Each line is 'ok|...', 'warn|...' or 'bad|...'."""
    if not gh_token():
        raise ValueError("Paste a GitHub token first.")
    lines = []
    try:
        lines.append("ok|Token accepted. It belongs to %s." % gh("GET", "/user").get("login", "your account"))
    except GHError as e:
        if e.code == 401:
            return ["bad|" + str(e)]
        lines.append("warn|Couldn't read your profile. That can be normal for a restricted token, so the repository test below is what matters.")
    repo = repo.strip()
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        return lines + ["warn|Type the repository as owner/name (like yourname/my-game) to test upload permission."]
    try:
        info = gh("GET", "/repos/" + repo)
    except GHError as e:
        return lines + ["bad|" + str(e)]
    lines.append("ok|Found %s (%s)." % (info.get("full_name", repo), "private" if info.get("private") else "public"))
    try:  # saves one tiny unreferenced object: no commit, nothing visible in the repo
        gh("POST", "/repos/%s/git/blobs" % repo, {"content": "permission test", "encoding": "utf-8"})
        lines.append("ok|Write access works. You are ready to upload.")
    except GHError as e:
        lines.append("bad|" + str(e))
    return lines


def gh_upload(folder, repo, create, message, prefix=""):
    if not gh_token():
        raise ValueError("Paste a GitHub token first.")
    if not re.match(r"^[\w.-]+(/[\w.-]+)?$", repo):
        raise ValueError("Type the repository as name or owner/name.")
    f, files, _, _ = gh_files(folder)
    if not files:
        raise ValueError("Nothing to upload in that folder.")
    if "/" not in repo:
        repo = gh("GET", "/user")["login"] + "/" + repo
    if create:
        gh("POST", "/user/repos", {"name": repo.split("/")[1], "private": True, "auto_init": True})
    info = gh("GET", "/repos/" + repo)
    branch = info["default_branch"]
    for i in range(4):  # a brand-new repo can take a moment to be ready
        try:
            base = gh("GET", "/repos/%s/git/ref/heads/%s" % (repo, branch))["object"]["sha"]
            break
        except ValueError:
            if i == 3:
                raise ValueError("Couldn't find the branch. If the repo is empty, add a README on github.com first.")
            time.sleep(1.5)
    tree0 = gh("GET", "/repos/%s/git/commits/%s" % (repo, base))["tree"]["sha"]
    pre = prefix.strip("/ ")
    tree = []
    import base64
    for rel in files:
        with open(f / rel, "rb") as fh:
            blob = gh("POST", "/repos/%s/git/blobs" % repo, {"content": base64.b64encode(fh.read()).decode(), "encoding": "base64"})
        tree.append({"path": (pre + "/" if pre else "") + rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    t = gh("POST", "/repos/%s/git/trees" % repo, {"base_tree": tree0, "tree": tree})
    c = gh("POST", "/repos/%s/git/commits" % repo, {"message": message or "Upload from Atlas", "tree": t["sha"], "parents": [base]})
    gh("PATCH", "/repos/%s/git/refs/heads/%s" % (repo, branch), {"sha": c["sha"]})
    return info["html_url"]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, t, a):
        self.skip += t in ("script", "style")

    def handle_endtag(self, t):
        self.skip -= t in ("script", "style")

    def handle_data(self, d):
        if self.skip <= 0 and d.strip():
            self.out.append(d.strip())


def extract(name, data):
    if name.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDFs need one extra install. In a terminal run: pip install pypdf")
        import io
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", "ignore")


def fetch_url(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Enter a full link starting with https://")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read(3000000).decode("utf-8", "ignore")
    p = _Text()
    p.feed(html)
    return "\n".join(p.out)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>try{document.documentElement.dataset.theme=localStorage.getItem("atlasTheme")||""}catch(e){}</script>
<title>Atlas</title>
<!--icon--><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'><rect width='1024' height='1024' rx='229' fill='%23141517'/><g transform='translate(512 512) scale(1.05) translate(-490 -490)' fill='none' stroke='%238aa0ff' stroke-width='36' stroke-linecap='round' stroke-linejoin='round'><path d='M490 232C400 232 312 320 312 450C312 570 340 700 490 700C640 700 668 570 668 450C668 320 580 232 490 232Z'/><path d='M312 448C262 462 250 568 322 592M668 448C718 462 730 568 658 592'/><path d='M410 690L388 750M573 690L592 750'/><path d='M380 432Q415 490 450 432M532 432Q567 490 602 432'/><path d='M452 578Q490 598 528 578'/><path d='M240 268L283 300M743 268L700 300M212 396H250M732 396H768'/></g></svg>">
<link rel="icon" type="image/png" sizes="128x128" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAMAAAD04JH5AAAAwFBMVEUUFReEmPIAAAANDAmLof50htQxN1FOWYkjJzVldLY7RGUaHCNcaqYTFBZsfcVWY5kTFRZCS3F9keYTFBZ7juEeISpHUXwTFBYUFBYTFBYpLkFgbqwVFRU4QF4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACinnDHAAAAMHRSTlP+/wD/////////////Tf//s///zf///yd9l///F/8AAAAAAAAAAAAAAAAAAAAAAAAKPb6zAAAF3UlEQVR42s1ba5uqIBDG5SIIXlMr2/3/f/NwVSwrS5QzH/ZpS5nXubzMgICfJTnn1+oEAsqpuubnRVVgQfslqG4PxeW8AkBegR2lyt8AyE9gZznlLwD8VuAAqX6fAcjBQZIvA7iAw+SyBOAKDpTrI4AKHCrVPYArOFiucwAXcLhcfAA5iCD5BOAXRJHfEUAVB0DlAOQgkuQWwCkWgJMBEM0A2gQgWgTYKAA/ZxBRzhLAJSaAiwRwigng9AOiekD6AORxAeTgGhfAFVRxAVTgFBfANvXQSBzoUm83kEwKoRwcjQJCnpW4SRIkJUnaoiT8QAwQENEoxZOgpBUEHAQBEpbMtDsMBTlEPRejSmMFNBmDURjc2nd2hQQj+8BY9BmRkvUCjybp70fg2wDUGGc+Aljbh8fp4LJQJSLtsf1BzOBS1ogt+gc1ZOoNmBqj4+w+4GRgMoOg4J65WiTv/94tMFP60O1Of5IuxTsEmfYOwny83QTGhrjgdkjq4Ukwgc/ik+nLC/t/akySbQlMWhgExqDWxC8G1CZCpboC9sZc2cbEEHoY/dA6zorXTjMItNJCXd6QrYkJpSFRy93YmIMVCJpOfiqRct52YoAZ1hRHtUGHtwOqONBO4AILDsOQkfqrfLEmpWijgFJ9Y0he1AbAa/DWzgRhOVn7dl1EYxsFYQWvNIAzQR3WBJoDUL9uUN7KaxncwQN05cUqXlse1gPFWw66n0JIAAaYat6uST6Y1lTG2CDYUjcTwW7ilvIxBNazOh4TsRdC3AT5LvEllSJTYXxqVGZnYRU6aoy10TN3pC2y9DzQfxCDNgoLFzrfTsmDvbdwSbA+rtU8pEkDClsrDl/Wg1KKwR/RI3gvtu5JX+PV8yZlepD6+0mITzY1AFJsC1/CyrEMwaX7jlFXi1iDcQ5AgEmJWafqqFIJpvICCeio18Q8l/2SenBNxjgkE0Ed1u5DQ2VvOM0N2j8yRaAOfhWqOmlwQPWmvGQjJePsT+m3fK+fV5bKpi7XFlAAmmCNmmuG2FgWJLYZc8SAp+80XdryOVCjZophB8COPeqaXaGM4l2EmiEEgq71AajmzDaHpY/Rfsf4DGUboCwxIeYV5ND4uvCJGVKhKkGX7zYqVFO13QTa6a1fD8lWlBB6zz0dIcPIUf3op+1tgRoL/aXzvFqaYH1m1JfXyUgVWxkAT9y6vn7C5tbNdIR1uNefzYYqbgr4t66TeV9gSkeSj8aCN6TqARoiCFQt5gCsH6vQ3lcJjLY2x4rz5SBelbfeb7QJkQaFHks/zKdFKUk+CZw3xQ3+YK3FOawMMSeaejiFRbJ+MJMznAbhATvV9ezRnB7xwIXOiOAgTChNMC3LzkajqV2rgyBL6SPmaXIMNhvNorBrZfmVScVdViQId4/1tK6dglQkrraezQZmCTHBWC+I+CnqVhNlPUACtch1e1cCzT2jdPmqXIEUbuka8kzg5G7hpfQQ+CvEJgkxS4Mu3ss4L+7zQPleSzJvPEzCBN9L0k6fmUAWJlmfpmk2+MWJIY5yj82Thbxe6v5xCAJeNMEfWsGGOmm3rNC/XQN9za1mQwPzXfSb+vTl09kOgey0fWbJ5zkCs6ixTwR6HCe7D/iihQzC/69ZGWHymOQQkmK+Y7OPWP5j821SiYfc7Hbavvr1/oVp1Go67Z6TtLDdIdtZvz/XJVikdVb3JZv2LTEAhwEwm7YIzXaxjwBwM73/wuZ1En6J+lmdzgjz9+/lx4Zl/V6TwGOvprioIymT5VDTtLiQc38HPu2etvCx7rdU9POu67jrzIcQ25Srm44laM36TZUNAF406njXeeBhGXgxPBOxO4ASNWh570Y2RE2IVak3MjToWajT9ogskPlHv/gpqBe++umpRH+ZrYoLoIr/QmP0Vzqjv9Qa/bXe+C82R3+1O/rL7fFf749+wCH+EY/oh1ziH/OJftAp/lGv+Ifd4h/3i3/gMf6Rz/iHXuMf+41/8Pk/OPr9Hxx+P/r4/z+UCzdNyPCCawAAAABJRU5ErkJggg=="><!--/icon-->
<style>
:root{color-scheme:dark;--bg:#121314;--panel:#1a1b1d;--ink:#e7e7e4;--mute:#8d9095;--line:#2b2d30;--accent:#8aa0ff;--onaccent:#0d1230;--me:#242a4a;--code:#0b0c0d;--err:#ff9b93}
:root[data-theme=light]{color-scheme:light;--bg:#f5f6f3;--panel:#e9ebe6;--ink:#1d2321;--mute:#626b68;--line:#d3d8d1;--accent:#3550e0;--onaccent:#fff;--me:#dfe5fb;--code:#e3e7df;--err:#b3261e}
:root[data-theme=galaxy]{--bg:#0f0a1e;--panel:#171030;--ink:#ece8f8;--mute:#9a92b8;--line:#2d2250;--accent:#b794ff;--onaccent:#1a0b3a;--me:#2e1f5c;--code:#0a0716}
[data-theme=galaxy] body{background:radial-gradient(1.2px 1.2px at 12% 18%,rgba(255,255,255,.75),transparent 60%),radial-gradient(1px 1px at 38% 62%,rgba(255,255,255,.6),transparent 60%),radial-gradient(1.4px 1.4px at 71% 27%,rgba(220,200,255,.8),transparent 60%),radial-gradient(1px 1px at 86% 74%,rgba(255,255,255,.55),transparent 60%),radial-gradient(1px 1px at 55% 92%,rgba(255,255,255,.5),transparent 60%),radial-gradient(900px 520px at 15% 0%,rgba(140,80,255,.18),transparent 70%),radial-gradient(800px 520px at 100% 100%,rgba(220,90,220,.11),transparent 70%),var(--bg);background-size:340px 340px,340px 340px,340px 340px,340px 340px,340px 340px,auto,auto,auto;background-attachment:scroll,scroll,scroll,scroll,scroll,fixed,fixed,scroll}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex}
button,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
aside{width:280px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px;gap:12px;overflow:auto}
h1{font:600 20px/1.2 Georgia,"Times New Roman",serif;margin:2px 0 0}
.btn{background:var(--accent);color:var(--onaccent);border:0;border-radius:8px;padding:9px 12px;font-weight:600}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
label{font-size:13px;color:var(--mute);display:block;margin-bottom:4px}
select,textarea,input:not([type=file]){width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px}
textarea{resize:vertical}
#chats{display:flex;flex-direction:column;gap:2px;flex:1;min-height:80px}
.chat{display:flex;align-items:center;border-radius:8px;padding:2px 2px 2px 10px}
.chat.on{background:var(--bg);box-shadow:inset 3px 0 0 var(--accent)}
.chat button.t{flex:1;text-align:left;background:none;border:0;padding:6px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat button.x{background:none;border:0;color:var(--mute);padding:4px 8px;border-radius:6px}
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:none;padding:10px 14px;border-bottom:1px solid var(--line)}
#msgs{flex:1;overflow:auto;padding:24px 16px}
.wrap{max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
.m{max-width:100%}
.m.user{align-self:flex-end;background:var(--me);padding:9px 14px;border-radius:14px 14px 3px 14px;max-width:85%;white-space:pre-wrap}
.m.assistant{font-family:Georgia,"Times New Roman",serif;font-size:16.5px;line-height:1.65}
pre{background:var(--code);padding:12px;border-radius:8px;overflow-x:auto;font-size:13.5px;line-height:1.45}
code{font-family:ui-monospace,Consolas,Menlo,monospace;font-size:.88em;background:var(--code);padding:1px 5px;border-radius:4px}
pre code{background:none;padding:0}
.empty{margin:12vh auto 0;max-width:520px;text-align:center}
.empty h2{font:600 26px/1.25 Georgia,serif;margin:0 0 8px}
.empty p{color:var(--mute);margin:0 0 18px}
.chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
#status{max-width:760px;width:100%;margin:0 auto;padding:0 16px;color:var(--err);font-size:14px;min-height:0}

.composer{border-top:1px solid var(--line);padding:12px 16px}
.row{max-width:760px;margin:0 auto;display:flex;gap:8px;align-items:flex-end}
.row textarea{flex:1;max-height:200px;resize:none}
#maker{position:fixed;inset:0;z-index:20;background:var(--bg);display:flex}
#maker[hidden]{display:none}
.mleft{width:360px;display:flex;flex-direction:column;border-right:1px solid var(--line);min-height:0}
.mhead{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid var(--line)}
#gmsgs{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.mleft .row{padding:12px;margin:0;max-width:none}
.mright{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
.mbar{display:flex;gap:6px;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line)}
.gwrap{flex:1;position:relative;background:#222;min-height:200px}
#gframe{width:100%;height:100%;border:0;background:#fff}
#gempty{position:absolute;inset:0;padding-top:20%;text-align:center;color:#999}
#gerr{padding:8px 14px;background:var(--me);display:flex;gap:10px;align-items:center;font-size:13px}
#gerr[hidden]{display:none}
#gcode{height:220px;font:12.5px ui-monospace,Consolas,monospace;border:0;border-top:1px solid var(--line);border-radius:0}
.hint{color:var(--mute);font-size:14px;margin:0}
@media (max-width:760px){#maker{flex-direction:column}.mleft{width:100%;height:45%;border-right:0;border-bottom:1px solid var(--line)}}
details{margin-bottom:6px}
summary{cursor:pointer;font-weight:600;font-size:13px;color:var(--mute);padding:6px 4px}
#vgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin:12px 0;max-height:55vh;overflow:auto}
.vc{font-size:12px}
.vc video{width:100%;aspect-ratio:16/9;background:#000;border-radius:8px}
.vc div{margin-top:4px;word-break:break-all}
.top{display:flex;align-items:center;justify-content:space-between;position:relative}
.icons{display:flex;gap:6px}
.icon{background:none;border:1px solid var(--line);border-radius:8px;width:36px;height:36px;display:grid;place-items:center;color:var(--ink)}
.icon:hover,.icon[aria-expanded="true"]{background:var(--bg)}
.icon svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#toolmenu{max-height:calc(100vh - 90px);overflow:auto;position:absolute;top:44px;left:0;right:0;z-index:30;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px;box-shadow:0 12px 32px rgba(0,0,0,.55)}
#toolmenu[hidden]{display:none}
#toolmenu [role="menuitem"]{display:block;width:100%;text-align:left;background:none;border:0;border-radius:8px;padding:7px 10px}
#toolmenu [role="menuitem"]:hover,#toolmenu [role="menuitem"]:focus-visible{background:var(--bg)}
#toolmenu b{display:block;font-weight:600;font-size:14px}
#toolmenu span{display:block;font-size:12px;color:var(--mute)}
.mgroup{display:flex;align-items:center;gap:6px;width:100%;background:none;border:0;font:inherit;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);padding:9px 10px 7px;cursor:pointer;border-radius:6px}
.mgroup:hover,.mgroup:focus-visible{color:var(--ink);background:var(--bg)}
.mgroup .mgname{flex:1;text-align:left}
.mgroup .mgchev{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round;transition:transform .15s;transform:rotate(0deg);flex:none}
.mgroup[aria-expanded="true"] .mgchev{transform:rotate(90deg)}
.mgbody{max-height:0;overflow:hidden;transition:max-height .2s ease}
.mgbody.open{max-height:1000px}
.chip{background:none;border:1px solid var(--line);border-radius:8px;color:var(--mute);font-size:12px;padding:6px 10px;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip:hover{color:var(--ink)}
.set{margin-bottom:14px}
.set .hint{font-size:12px;margin-top:4px}
dialog::backdrop{background:rgba(0,0,0,.62)}
.ibox{flex:1;display:flex;align-items:flex-end;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding-right:4px;min-width:0}
.ibox:focus-within{border-color:var(--accent)}
.ibox textarea{flex:1;width:auto;min-width:0;border:0;background:transparent;outline:none;padding:10px 12px;resize:none;max-height:200px}
.itools{display:flex;gap:2px;align-items:center;padding-bottom:4px}
.ibtn{background:none;border:0;color:var(--mute);width:34px;height:34px;border-radius:8px;display:grid;place-items:center}
.ibtn:hover{color:var(--ink);background:var(--panel)}
.ibtn.on{background:var(--accent);color:var(--onaccent)}
.ibtn svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.crow{display:flex;gap:8px;align-items:center;padding:4px 2px;font-size:13px;color:var(--ink);margin:0;word-break:break-all}
.crow input{width:auto}
#clist:empty{display:none}
#clist{max-height:32vh;overflow:auto;margin:10px 0;border:1px solid var(--line);border-radius:8px;padding:4px 8px}
dialog a{color:var(--accent)}
dialog ol{padding-left:20px;font-size:13px;margin:6px 0}
dialog li{margin:5px 0}
dialog code{font-size:12px}
.step{margin:16px 0 6px}
.acts{display:flex;gap:2px;align-items:center;margin-top:6px;margin-left:-6px}
.abtn{background:none;border:0;color:var(--mute);width:30px;height:30px;border-radius:8px;display:grid;place-items:center}
.abtn:hover{color:var(--ink);background:var(--panel)}
.abtn.on{color:var(--accent)}
.abtn svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.abtn svg.face{width:22px;height:22px;stroke-width:40}
input[type=range]{padding:0;border:0;background:none;accent-color:var(--accent)}
.vwrap{position:relative;margin-top:0!important}
.vexp{position:absolute;top:6px;right:6px;width:30px;height:30px;border:0;border-radius:8px;background:rgba(0,0,0,.55);color:#fff;display:grid;place-items:center;opacity:.85}
.vexp:hover,.vexp:focus-visible{opacity:1;background:rgba(0,0,0,.8)}
.vexp svg,#vvclose svg,.vvnav svg{stroke:#fff;fill:none;stroke-linecap:round;stroke-linejoin:round}
.vexp svg{width:16px;height:16px;stroke-width:2}
#vview{position:fixed;inset:0;z-index:100;background:#000;display:flex;align-items:center;justify-content:center;outline:none}
#vview[hidden]{display:none}
#vvstage{width:100%;height:100%;display:flex;align-items:center;justify-content:center}
#vvstage video,#vvstage img{width:100%;height:100%;object-fit:contain}
#vvbar{position:absolute;top:16px;left:16px;z-index:3;display:flex;gap:10px;align-items:stretch}
#vvadd,#vvren{display:flex;align-items:center;gap:8px;padding:10px 14px;border:0;border-radius:10px;background:rgba(0,0,0,.6);color:#fff;font-size:15px}
#vvadd:hover,#vvadd:focus-visible,#vvren:hover,#vvren:focus-visible{background:rgba(0,0,0,.85)}
#vvadd svg,#vvren svg{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#vvpick,#vvrn{position:absolute;top:72px;left:16px;z-index:4;width:300px;max-height:65vh;overflow:auto;padding:12px;border-radius:12px;background:rgba(18,18,20,.97);border:1px solid #333;color:#fff}
.vvph{font-weight:600;margin-bottom:8px}
#vvpl button{display:block;width:100%;text-align:left;margin:3px 0;padding:9px 10px;border:0;border-radius:8px;background:#26262a;color:#fff}
#vvpl button:hover,#vvpl button:focus-visible{background:#3a3a44}
.vvpn{display:flex;gap:6px;margin-top:10px}
.vvpn input{flex:1;min-width:0;padding:8px;background:#111;color:#fff;border:1px solid #444;border-radius:8px}
.vvpn button{border:0;border-radius:8px;padding:8px 10px;background:var(--accent);color:var(--onaccent);font-weight:600}
#vvrnext{align-self:center;color:#bbb;font-size:13px}
#vvpmsg,#vvrnmsg{margin-top:6px;font-size:12px;color:#ffb4a9}
@media (max-width:1100px){#vvtitle{top:72px}}
#vvsel{display:flex;align-items:center;gap:10px;margin:0;padding:10px 14px;border-radius:10px;background:rgba(0,0,0,.6);color:#fff;font-size:15px;cursor:pointer}
#vvsel input{width:26px;height:26px;margin:0;padding:0;accent-color:var(--accent);cursor:pointer}
#vvtitle{position:absolute;top:16px;left:50%;transform:translateX(-50%);z-index:3;max-width:min(40%,480px);color:#fff;font-size:14px;text-align:center;white-space:pre-line;overflow:hidden;text-overflow:ellipsis;text-shadow:0 1px 4px #000}
#vvclose{position:absolute;top:16px;right:16px;z-index:3;width:44px;height:44px;border:0;border-radius:10px;background:rgba(0,0,0,.6);display:grid;place-items:center}
#vvclose svg{width:22px;height:22px;stroke-width:2.2}
.vvnav{position:absolute;top:50%;transform:translateY(-50%);z-index:3;width:56px;height:96px;border:0;border-radius:12px;background:rgba(0,0,0,.5);display:grid;place-items:center}
.vvnav:hover,.vvnav:focus-visible,#vvclose:hover{background:rgba(0,0,0,.8)}
.vvnav svg{width:34px;height:34px;stroke-width:2.2}
#vvprev{left:12px}#vvnext{right:12px}
#fsec{margin:12px 0 6px}
.frow{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.fnew{background:none;border:0;color:var(--accent);font-weight:600;font-size:14px;display:flex;align-items:center;gap:8px;padding:4px 2px;margin:0!important}
.fnew.sub{color:var(--mute);font-weight:500}
.fnew[hidden]{display:none}
.fcard>span{display:grid;place-items:center;flex:none}
.fnew svg,.fcard svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
#fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
#fgrid:empty{display:none}
.fcard{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid transparent;border-radius:10px;padding:10px;cursor:pointer;min-width:0;color:var(--ink)}
.fcard:hover{border-color:var(--line)}
.fcard.drop{border-color:var(--accent);background:var(--me)}
.fcard input[type=checkbox]{width:18px;height:18px;margin:0;padding:0;flex:none;accent-color:var(--accent);cursor:pointer}
.fcard .fname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.fcard .fedit{flex:1;min-width:0;padding:4px 8px;background:var(--bg);border:1px solid var(--accent);border-radius:6px;color:var(--ink)}
.fcard .fdots,.fcard .fok{background:none;border:0;color:var(--mute);width:28px;height:28px;border-radius:6px;display:grid;place-items:center;flex:none;padding:0}
.fcard .fdots:hover,.fcard .fok:hover{background:var(--bg);color:var(--ink)}
.fcard .fdots svg{fill:currentColor;stroke:none;width:18px;height:18px}
#fmenu{position:fixed;z-index:60;min-width:160px;padding:4px;border-radius:10px;background:var(--panel);border:1px solid var(--line);box-shadow:0 10px 28px rgba(0,0,0,.5)}
#fmenu[hidden]{display:none}
#fmenu button{display:block;width:100%;text-align:left;background:none;border:0;padding:8px 10px;border-radius:6px;color:var(--ink)}
#fmenu button:hover,#fmenu button:focus-visible{background:var(--bg)}
.dragghost{position:fixed;top:-200px;left:-200px;padding:8px 12px;border-radius:8px;background:var(--accent);color:var(--onaccent);font-weight:600}
.vc{border-radius:10px}
.vc.sel{outline:2px solid var(--accent);outline-offset:2px}
.vgrip{position:absolute;top:6px;left:6px;width:30px;height:30px;border-radius:8px;background:rgba(0,0,0,.55);display:grid;place-items:center;cursor:grab;opacity:.85}
.vgrip:hover{opacity:1;background:rgba(0,0,0,.8)}
.vgrip svg{width:16px;height:16px;fill:#fff;stroke:none}
.switch{display:flex;align-items:center;gap:10px;color:var(--ink)!important;font-size:14px!important;cursor:pointer;margin:0 0 4px!important;position:relative}
.switch input{position:absolute;opacity:0;width:0!important;height:0;margin:0;padding:0;border:0}
.switch .track{width:44px;height:24px;border-radius:12px;background:var(--line);position:relative;flex:none;transition:background .15s}
.switch .track::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:var(--mute);transition:transform .15s,background .15s}
.switch input:checked+.track{background:var(--accent)}
.switch input:checked+.track::after{transform:translateX(20px);background:var(--onaccent)}
.switch input:focus-visible+.track{outline:2px solid var(--accent);outline-offset:2px}
.vname{display:flex;align-items:flex-start;gap:4px}
.vname label{flex:1;min-width:0}
.vren{background:none;border:0;color:var(--mute);width:26px;height:26px;border-radius:6px;display:grid;place-items:center;flex:none;padding:0;margin-top:1px}
.vren:hover,.vren:focus-visible{background:var(--panel);color:var(--ink)}
.vren svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.vc .vedit{width:calc(100% - 46px);display:inline-block;margin:0;padding:3px 6px;font-size:12px}
.cgrid{display:grid;grid-template-columns:270px 1fr;gap:18px}
@media (max-width:800px){.cgrid{grid-template-columns:1fr}}
#codefiles{max-height:52vh;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px 8px;margin-top:8px}
#codefiles:empty{display:none}
#codefiles .crow{font-size:12px}
.cnote{white-space:pre-wrap;margin:8px 0;font-size:14px;color:var(--ink)}
.cchg{border:1px solid var(--line);border-radius:10px;padding:6px 10px;margin:8px 0}
.cchg .crow{font-size:14px}
.cbadge{font-size:11px;padding:1px 8px;border-radius:10px;background:var(--panel);color:var(--mute);margin-left:6px}
.cbadge.warn{background:#5a3b12;color:#ffd9a0}
.cwhy{font-size:12px;color:var(--mute);margin:2px 0 4px 26px}
pre.diff{max-height:300px;overflow:auto;font-size:12px;line-height:1.4;margin:6px 0;padding:6px;white-space:pre-wrap;word-break:break-all}
.dadd{background:rgba(70,190,110,.2)}
.ddel{background:rgba(240,90,90,.2)}
.dctx{color:var(--mute)}
#autolines{font-size:14px;padding-left:22px}
#autolines li{margin:3px 0}
#cytracks{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:14px 0}
.cytrack{text-align:left;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;cursor:pointer;color:var(--ink)}
.cytrack:hover{border-color:var(--accent)}
.cytrack .ct-top{display:flex;justify-content:space-between;align-items:center;font-size:28px}
.cytrack .ct-title{font-weight:700;margin-top:8px}
.cytrack .ct-bar{height:6px;border-radius:3px;background:var(--line);margin-top:10px;overflow:hidden}
.cytrack .ct-bar i{display:block;height:100%;background:var(--accent)}
.cytrack .ct-n{font-size:12px;color:var(--mute);margin-top:4px}
.cysub{border-top:1px solid var(--line);margin-top:4px;padding-top:16px}
.cystats{font-size:13px;color:var(--mute);margin:8px 0}
.cybar{display:flex;align-items:center;gap:14px;margin-bottom:16px}
.cyprog{flex:1;height:8px;border-radius:4px;background:var(--line);overflow:hidden}
#cyprogfill{height:100%;width:0;background:var(--accent);transition:width .2s}
.cycard h3{font:700 20px/1.3 Georgia,serif;margin:0 0 10px}
.cycard p{line-height:1.55;margin:0 0 10px}
.cyopt{display:block;width:100%;text-align:left;background:var(--panel);border:2px solid var(--line);border-radius:10px;padding:11px 14px;margin:7px 0;color:var(--ink);font-size:15px}
.cyopt:hover:not(:disabled){border-color:var(--accent)}
.cyopt:disabled{opacity:.85}
.cyopt.right{border-color:#2e8b57;background:rgba(46,139,87,.18)}
.cyopt.wrong{border-color:#c0392b;background:rgba(192,57,43,.18)}
.cyfeed{margin-top:12px;padding:12px 14px;border-radius:10px;font-size:14px;line-height:1.5}
.cyfeed.right{background:rgba(46,139,87,.15);border:1px solid #2e8b57}
.cyfeed.wrong{background:rgba(192,57,43,.15);border:1px solid #c0392b}
.cyrow{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.cyinput{font-size:16px;padding:10px 12px;border-radius:10px;border:2px solid var(--line);background:var(--bg);color:var(--ink);width:100%;max-width:280px}
.cyinput.right{border-color:#2e8b57}.cyinput.wrong{border-color:#c0392b}
.cyask{background:none;border:1px solid var(--line);border-radius:8px;padding:8px 12px;color:var(--accent);font-weight:600;font-size:13px}
.cyask:hover{background:var(--panel)}
.cyaskbox{margin-top:12px;border:1px dashed var(--line);border-radius:10px;padding:10px 12px}
.cyaskbox textarea{width:100%;margin-top:6px}
.cytxt{white-space:pre-wrap;font-size:14px;line-height:1.55;margin-top:6px;color:var(--ink)}
.cywho{font-size:12px;color:var(--mute);margin-bottom:2px}
.cyq .q{font-weight:600;font-size:17px;margin-bottom:12px}
.mnew{display:flex;gap:8px;margin:10px 0 16px}
.mnew input{flex:1}
.mfold{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.mfold.drop{border-color:var(--accent);background:var(--me)}
.mfhead{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.mfhead .mfname{font-weight:700;flex:1}
.mfhead input.mfedit{flex:1;padding:4px 8px;border:1px solid var(--accent);border-radius:6px;background:var(--bg);color:var(--ink)}
.mfhead button{background:none;border:0;color:var(--mute);width:26px;height:26px;border-radius:6px;flex:none}
.mfhead button:hover{background:var(--panel);color:var(--ink)}
.mfhead button svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.mchips{display:flex;flex-wrap:wrap;gap:8px;min-height:30px}
.mchip{display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;cursor:grab}
.munsorted{border-top:1px dashed var(--line);padding-top:12px;margin-top:4px}
.munsorted.drop{background:var(--me);border-radius:10px}
.mgrouplabel{font-size:12px;color:var(--mute);margin-bottom:6px}
#status.ok{color:var(--mute)}
.doc{display:flex;gap:6px;align-items:center;font-size:13px;margin-bottom:4px}
.doc span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dialog{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:12px;max-width:560px;width:92%;position:relative;padding-top:46px}
.xclose{position:absolute;top:12px;left:14px;z-index:6;width:30px;height:30px;border:0;border-radius:8px;background:none;color:var(--mute);display:grid;place-items:center}
.xclose:hover{background:var(--panel);color:var(--ink)}
.xclose svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#plan{max-height:260px;overflow:auto;font-size:13px;margin:12px 0}
dialog .btn{margin-right:6px;margin-top:8px}
@media (max-width:760px){
 aside{position:fixed;inset:0 20% 0 0;z-index:5;transform:translateX(-100%);transition:transform .2s}
 body.open aside{transform:none}
 header{display:block}
}
@media (prefers-reduced-motion:reduce){aside{transition:none}}
</style></head>
<body>
<aside>
 <div class="top">
  <h1>Atlas</h1>
  <div class="icons">
   <button class="icon" id="toolsbtn" aria-label="Tools menu" aria-haspopup="true" aria-expanded="false" aria-controls="toolmenu"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h18"/></svg></button>
   <button class="icon" id="gearbtn" aria-label="Settings"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
  </div>
  <div id="toolmenu" role="menu" hidden>
   <button role="menuitem" id="makegame"><b>Make a game</b><span>Describe it, play it, edit it live</span></button>
   <button role="menuitem" id="codebtn"><b>Code helper</b><span>Write and edit real code files</span></button>
   <button role="menuitem" id="cyberbtn"><b>Cyber Trainer</b><span>Bite-size cybersecurity lessons + subnetting drills</span></button>
   <button role="menuitem" id="autobtn"><b>Computer helper</b><span>Let Atlas click and type for you</span></button>
   <button role="menuitem" id="vids"><b>Videos and images</b><span>Preview, pick and send to a folder</span></button>
   <button role="menuitem" id="convbtn"><b>Convert files</b><span>Change video and image formats</span></button>
   <button role="menuitem" id="tidy"><b>Organize a folder</b><span>Sort Downloads by type, with undo</span></button>
   <button role="menuitem" id="docsbtn"><b>Reference files</b><span>Guides and notes the AI can read</span></button>
   <button role="menuitem" id="sharebtn"><b>Share with phone</b><span>Scan a code to move files</span></button>
   <button role="menuitem" id="gh"><b>Upload to GitHub</b><span>Push a folder as one commit</span></button>
  </div>
 </div>
 <button class="btn" id="new">New chat</button>
 <input id="q" placeholder="Search chats" aria-label="Search chats">
 <button class="btn ghost" id="sortall">Sort chats by topic</button>
 <div id="chats"></div>
 <button class="chip" id="modelchip" title="Open settings"></button>
</aside>
<main>
 <header><button class="btn ghost" id="menu">Menu</button></header>
 <div id="msgs"><div class="wrap" id="list"></div></div>
 <div id="status" role="status"></div>
 <div class="composer"><div id="pend" style="max-width:760px;margin:0 auto 8px;display:flex;gap:8px;flex-wrap:wrap"></div><div class="row">
  <div class="ibox"><textarea id="input" rows="1" placeholder="Message Atlas" aria-label="Message"></textarea>
   <div class="itools"><button class="ibtn" id="attach" title="Attach an image or a video" aria-label="Attach an image or a video"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button><input type="file" id="afile" accept="image/*,video/*" multiple hidden><button class="ibtn" id="vbtn" title="Voice mode" aria-label="Voice mode" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v4M8 23h8"/></svg></button></div></div>
  <button class="btn" id="send">Send</button>
 </div></div>
</main>
<div id="maker" hidden>
 <section class="mleft">
  <div class="mhead"><strong>Game maker</strong><button class="btn ghost" id="gclose">Close</button></div>
  <div id="gmsgs"></div>
  <div class="row"><textarea id="ginput" rows="2" placeholder="Describe a game, or a change to make"></textarea><button class="btn" id="gsend">Send</button></div>
 </section>
 <section class="mright">
  <div class="mbar"><button class="btn ghost" id="gundo">Undo edit</button><button class="btn ghost" id="gview">View code</button><button class="btn ghost" id="grun" hidden>Run my edits</button><button class="btn ghost" id="gdl">Download</button><button class="btn ghost" id="gnew">Start over</button></div>
  <div class="gwrap"><iframe id="gframe" sandbox="allow-scripts" title="Game preview"></iframe><div id="gempty">Your game appears here</div></div>
  <div id="gerr" hidden><span id="gerrt"></span><button class="btn" id="gfix">Ask AI to fix this</button></div>
  <textarea id="gcode" hidden spellcheck="false"></textarea>
 </section>
</div>
<dialog id="vdlg" style="max-width:980px"><h2>Videos and images</h2>
 <p>Preview your files, tick the ones you want, then send them to a folder of your choice.</p>
 <label for="vfolder">Folder with your videos</label><input id="vfolder" value="~/Videos">
 <label style="margin-top:6px"><input type="checkbox" id="vdeep" style="width:auto"> Include videos inside subfolders</label>
 <button class="btn" id="vload">Show my files</button><button class="btn ghost" id="vall">Select all</button><button class="btn ghost" id="vnone">Select none</button>
 <label for="vdest" style="margin-top:8px">Send the ticked videos to this folder (it is created if needed)</label><input id="vdest" placeholder="~/Videos/Highlights">
 <label for="vmode" style="margin-top:8px">How</label><select id="vmode"><option value="link">Keep the originals too (no extra space on the same drive)</option><option value="copy">Copy (originals stay, uses more space)</option><option value="move">Move the files</option></select>
 <div id="vmsg" role="status" style="margin-top:8px"></div>
 <div id="fsec" hidden>
  <div class="frow">
   <button class="fnew" id="fnew"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M12 11v6M9 14h6"/></svg><span>Create Folder</span></button>
   <button class="fnew sub" id="fup" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg><span>Up one level</span></button>
   <button class="fnew sub" id="fdel" hidden><span>Delete ticked folders</span></button>
  </div>
  <div id="fgrid"></div>
 </div>
 <div id="vgrid"></div>
 <button class="btn" id="vgo">Send ticked videos</button><button class="btn ghost" id="vundo">Undo last run</button><button class="btn ghost" id="vclose">Close</button>
 <div id="fmenu" hidden role="menu"></div>
 <div id="vview" hidden tabindex="-1" role="dialog" aria-label="Full screen viewer">
  <div id="vvbar">
   <label id="vvsel" title="Select this file (S)"><input type="checkbox" id="vvcb"><span>Select</span></label>
   <button id="vvren" aria-label="Rename this file" title="Rename"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg></button>
   <button id="vvadd" aria-haspopup="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M12 11v6M9 14h6"/></svg><span>Add to folder</span></button>
  </div>
  <div id="vvrn" hidden>
   <div class="vvph">Rename</div>
   <div class="vvpn"><input id="vvrname" type="text" maxlength="120" aria-label="New file name"><span id="vvrnext"></span><button id="vvrnok">Save</button></div>
   <div id="vvrnmsg"></div>
  </div>
  <div id="vvpick" hidden>
   <div class="vvph">Add to folder</div>
   <div id="vvpl"></div>
   <div class="vvpn"><input id="vvpname" type="text" placeholder="New folder name" maxlength="80"><button id="vvpmk">Create and add</button></div>
   <div id="vvpmsg"></div>
  </div>
  <div id="vvtitle"></div>
  <button id="vvclose" aria-label="Close full screen"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <button class="vvnav" id="vvprev" aria-label="Previous file"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg></button>
  <button class="vvnav" id="vvnext" aria-label="Next file"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg></button>
  <div id="vvstage"></div>
 </div>
</dialog>
<dialog id="ghdlg" style="max-width:640px"><h2>Upload to GitHub</h2>
 <p class="hint">Uploads a folder to a GitHub repository as one commit. Three steps.</p>
 <div class="step"><b>1. Make a token</b> <span class="hint">(a key that lets this app upload for you)</span></div>
 <details open><summary>Recommended: a fine-grained token</summary>
  <ol>
   <li>Open <a href="https://github.com/settings/personal-access-tokens/new" data-ext>GitHub's fine-grained token page</a> and sign in.</li>
   <li>Token name: <b>Atlas</b>. Expiration: 30 or 90 days is a good choice.</li>
   <li>Resource owner: your account, or the organization if the repo belongs to one.</li>
   <li>Repository access: choose <b>Only select repositories</b> and pick the repo you will upload to.</li>
   <li>Under <b>Repository permissions</b>, set <b>Contents</b> to <b>Read and write</b>. That is the only permission needed. (Metadata: Read is added automatically.)</li>
   <li>Click <b>Generate token</b> and copy it. It starts with <code>github_pat_</code> and GitHub shows it only once.</li>
  </ol>
  <p class="hint">Create the repo on github.com first (New repository, tick "Add a README"). Fine-grained tokens usually can't create repos, and the repo needs at least one commit. If it belongs to an organization with single sign-on, click Configure SSO on the token page and authorize it.</p>
 </details>
 <details><summary>Or: a classic token (can also create new repos)</summary>
  <ol>
   <li>Open <a href="https://github.com/settings/tokens/new?scopes=repo&description=Atlas" data-ext>the classic token page</a>.</li>
   <li>Tick the one box named <b>repo</b>. Nothing else is needed. (For public repos only, <b>public_repo</b> is enough.)</li>
   <li>Pick an expiration, click <b>Generate token</b>, and copy it. It starts with <code>ghp_</code>.</li>
  </ol>
  <p class="hint">A classic token can reach all of your repos, so use the fine-grained one if you can.</p>
 </details>
 <div class="step"><b>2. Fill in the details</b></div>
 <label for="ghtoken">GitHub token <span id="ghhas"></span></label><input id="ghtoken" type="password" placeholder="Paste once. It is saved on this computer only.">
 <label for="ghfolder" style="margin-top:8px">Folder to upload</label><input id="ghfolder" placeholder="~/projects/my-game">
 <label for="ghrepo" style="margin-top:8px">Repository</label><input id="ghrepo" placeholder="yourname/my-game">
 <p class="hint">Type it as owner/name. The owner is your GitHub username.</p>
 <label style="margin-top:8px"><input type="checkbox" id="ghnew" style="width:auto"> Create it as a new private repository (needs a classic token)</label>
 <label for="ghmsg" style="margin-top:8px">Commit message</label><input id="ghmsg" value="Upload from Atlas">
 <div class="step"><b>3. Test, preview, upload</b></div>
 <button class="btn ghost" id="ghtest">Test token and repository</button><button class="btn ghost" id="ghsug">Suggest a message</button><button class="btn" id="ghprev">Preview files</button>
 <p class="hint">The test saves one tiny invisible object. It makes no commit and changes nothing you can see in the repo.</p>
 <div id="ghlist" role="status" style="margin-top:10px;max-height:220px;overflow:auto;font-size:13px"></div>
 <button class="btn" id="ghgo" hidden>Upload</button><button class="btn ghost" id="ghforget">Forget token</button><button class="btn ghost" id="ghclose">Close</button>
</dialog>
<dialog id="shdlg"><h2>Share with your phone</h2>
 <p>Your phone and this computer need to be on the same Wi-Fi. Start sharing, scan the code with your phone's camera, and a page opens where you can send files to this computer and download files from it. Anyone on your Wi-Fi who has the link could use it, so it turns off after 30 minutes of no use, or when you stop it. Windows may ask to allow network access the first time. Choose private networks.</p>
 <div id="shoff">
  <label for="shrecv">Files from your phone are saved in</label><input id="shrecv">
  <button class="btn" id="shstart">Start sharing</button>
 </div>
 <div id="shon" hidden>
  <canvas id="shqr" style="background:#fff;border-radius:8px;display:block;margin:8px auto" aria-label="QR code to scan with your phone"></canvas>
  <div id="shurl" style="text-align:center;font-size:12px;word-break:break-all;user-select:all"></div>
  <strong style="display:block;margin-top:12px">Received from your phone</strong><div id="shgot" style="font-size:13px"></div>
  <button class="btn ghost" id="shopen">Open that folder</button>
  <strong style="display:block;margin-top:12px">Available to download on your phone</strong><div id="shout" style="font-size:13px"></div>
  <button class="btn ghost" id="shadd">Add files to send</button><input type="file" id="shfile" multiple hidden>
  <button class="btn ghost" id="shstop">Stop sharing</button>
 </div>
 <div id="shmsg" role="status" style="margin-top:8px"></div>
 <button class="btn ghost" id="shclose">Close</button>
</dialog>
<dialog id="setdlg"><h2>Settings</h2>
 <div class="set"><label>Tools menu</label><button class="btn ghost" id="mopenbtn" style="margin:0">Customize tools menu</button>
  <p class="hint">Create folders for the hamburger-menu tools, rename or delete them, and drag tools between folders. You can also just tell Atlas in chat &mdash; try "create a folder called Game Dev" or "move Code helper into Files".</p></div>
 <div class="set"><label for="theme">Theme</label><select id="theme"><option value="">Default (dark)</option><option value="light">Light</option><option value="galaxy">Galaxy (purple)</option></select></div>
 <div class="set"><label for="model">Model</label><select id="model"></select>
  <p class="hint">The AI that answers you. Smaller models are faster.</p></div>
 <div class="set"><label for="mode">Mode</label><select id="mode"><option value="chat">Chat</option><option value="game">Game helper</option></select>
  <div id="gamebox" hidden>
   <label for="game" style="margin-top:8px">Game</label><input id="game" placeholder="e.g. Elden Ring">
   <label for="notes" style="margin-top:8px">My character or build</label><textarea id="notes" rows="4" placeholder="Level, class, gear, goals"></textarea>
  </div></div>
 <div class="set"><label for="preset">Personality</label><select id="preset"></select>
  <p class="hint">A starting point for the instructions below.</p></div>
 <div class="set"><label for="system">Instructions it always follows</label><textarea id="system" rows="4"></textarea></div>
 <div class="set"><label for="voicesample">How I write</label><textarea id="voicesample" rows="3" placeholder="Paste a few things you wrote: texts, emails, posts."></textarea>
  <p class="hint">Used by "Make it sound like me". It also learns from your chat messages.</p></div>
 <div class="set"><label for="voicesel">Voice for reading aloud</label><select id="voicesel"></select>
  <label for="vrate" style="margin-top:8px">Speed <span id="vratev"></span></label><input type="range" id="vrate" min="0.6" max="1.6" step="0.1" value="1">
  <label for="vpitch" style="margin-top:8px">Pitch <span id="vpitchv"></span></label><input type="range" id="vpitch" min="0.6" max="1.6" step="0.1" value="1">
  <button class="btn ghost" id="vprev">Preview voice</button>
  <p class="hint">The voices come from your computer and browser. Chrome and Edge usually have several, including natural-sounding ones.</p></div>
 <div class="set"><label>Ollama updates</label>
  <label class="switch"><input type="checkbox" id="ollauto" role="switch"><span class="track"></span><span>Update Ollama automatically</span></label>
  <div id="ollinfo" class="hint" role="status" style="margin:6px 0">Not checked yet.</div>
  <button class="btn ghost" id="ollcheck">Check for updates</button><button class="btn ghost" id="olldl">Download latest Ollama</button>
  <p class="hint">When this is on, Atlas checks each time it starts and installs new Ollama versions for you (Windows). On Mac and Linux it tells you when one is out, and you use the Download button. Ollama's own app can also offer updates from its tray icon.</p></div>
 <div class="set"><label>Image and video reading</label>
  <button class="btn ghost" id="vtbtn" style="margin:0">Test image and video reading</button>
  <div id="vtres" role="status" style="font-size:13px;margin-top:8px"></div>
  <p class="hint">Checks that a model on your computer can really see pictures and video clips. If it fails, the message says what to install.</p></div>
 <div class="set"><label>App icon</label>
  <div style="display:flex;gap:12px;align-items:center">
   <img id="icprev" width="64" height="64" alt="Current app icon" style="background:repeating-conic-gradient(#7a7a7a 0 25%,#5c5c5c 0 50%) 0 0/16px 16px">
   <div><button class="btn ghost" id="icpick" style="margin:0">Choose an image</button><input type="file" id="icfile" accept="image/*" hidden></div>
  </div>
  <div id="icopts" hidden>
   <label for="icfit" style="margin-top:8px">How to fit it</label><select id="icfit"><option value="fill">Fill the square (crops the edges)</option><option value="fit">Keep the whole picture (adds padding)</option></select>
   <label for="icshape" style="margin-top:8px">Shape</label><select id="icshape"><option value="rounded">Rounded square</option><option value="circle">Circle</option><option value="square">Square</option></select>
   <label for="icbg" style="margin-top:8px">Background color</label><input type="color" id="icbg" value="#141517" style="height:36px;padding:2px">
   <label style="margin-top:6px"><input type="checkbox" id="ictrans" style="width:auto"> Transparent background</label>
   <button class="btn" id="icsave">Save icon</button>
  </div>
  <div id="icmsg" class="hint" role="status" style="margin-top:6px"></div>
  <button class="btn ghost" id="icshort">Update my desktop shortcut</button><button class="btn ghost" id="icreset">Reset to default</button>
  <p class="hint">Any picture works. It is cropped or padded to a perfect square and saved at every size your computer needs (16 to 512 pixels). The window picture changes right away, and the taskbar picture the next time you open Atlas.</p></div>
 <div class="smsg" role="status"></div>
 <button class="btn" id="setclose">Done</button>
</dialog>
<dialog id="menudlg" style="max-width:640px"><h2>Customize Tools Menu</h2>
 <p class="hint">Group the hamburger-menu tools however makes sense to you. Drag a tool chip into a folder, rename or delete folders with the buttons on each one, or just ask Atlas in chat to do it for you.</p>
 <div class="mnew"><input id="mnewname" placeholder="New folder name" maxlength="40" aria-label="New folder name"><button class="btn" id="mnewgo">Create folder</button></div>
 <div id="mfolders"></div>
 <div class="munsorted"><div class="mgrouplabel">Not in a folder (shown under "More" in the menu)</div><div id="munsorted" class="mchips"></div></div>
 <div class="smsg" role="status" id="mmsg"></div>
 <button class="btn" id="mdone">Done</button>
</dialog>
<dialog id="docdlg"><h2>Reference files</h2>
 <p class="hint">Add guides, notes, PDFs or web pages. The AI looks inside the ticked ones when it answers.</p>
 <div id="docs" style="margin:12px 0"></div>
 <button class="btn" id="addfile">Add file</button><button class="btn ghost" id="addurl">Add web page</button>
 <input type="file" id="file" multiple hidden>
 <div class="smsg" role="status" style="margin-top:8px"></div>
 <button class="btn ghost" id="docclose">Close</button>
</dialog>
<dialog id="cvdlg" style="max-width:720px"><h2>Convert files</h2>
 <p class="hint">Change videos and images to another format, like mp4 to gif or png to jpg. Your originals are never touched. This needs ffmpeg (the app tells you how to get it if it is missing).</p>
 <label for="cfolder" style="margin-top:10px">Folder with your files</label><input id="cfolder" value="~/Videos">
 <label style="margin-top:6px"><input type="checkbox" id="cdeep" style="width:auto"> Include subfolders</label>
 <button class="btn" id="cload">Show my files</button><button class="btn ghost" id="call">Select all</button><button class="btn ghost" id="cnone">Select none</button>
 <div id="clist"></div>
 <label for="cfmt">Convert the ticked files to (type any format)</label><input id="cfmt" list="fmts" placeholder="mp4, webm, gif, mp3, png, jpg, webp ...">
 <datalist id="fmts"><option value="mp4"><option value="mkv"><option value="webm"><option value="mov"><option value="avi"><option value="gif"><option value="mp3"><option value="wav"><option value="png"><option value="jpg"><option value="webp"><option value="bmp"><option value="tiff"><option value="ico"><option value="avif"></datalist>
 <label for="cdest" style="margin-top:8px">Save converted files in (blank = a Converted folder next to the originals)</label><input id="cdest">
 <div id="cmsg" role="status" style="margin:8px 0"></div>
 <button class="btn" id="cgo">Convert ticked</button><button class="btn ghost" id="cclose">Close</button>
</dialog>
<dialog id="codedlg" style="max-width:1100px"><h2>Code helper</h2>
 <p class="hint">Pick a project folder, tick the files the AI should read, say what you want, and review every change before anything is written. Nothing is run. You can undo the last apply.</p>
 <div class="cgrid">
  <div>
   <label for="codefolder">Project folder</label><input id="codefolder" placeholder="~/projects/my-app">
   <button class="btn" id="codeopen">Open project</button>
   <div id="codefiles" role="group" aria-label="Project files"></div>
  </div>
  <div>
   <label for="codereq">What do you want done?</label>
   <textarea id="codereq" rows="4" placeholder="e.g. Add a function that ... / Fix the bug in ... / Create a README for this project"></textarea>
   <div id="codeimgs" style="display:flex;gap:6px;margin:6px 0"></div>
   <button class="btn" id="codego">Ask</button><button class="btn ghost" id="codeimg">Attach image</button><input type="file" id="codeimgfile" accept="image/*" hidden><button class="btn ghost" id="codeundo">Undo last apply</button><button class="btn ghost" id="codeclose">Close</button>
   <div id="codemsg" role="status" style="margin:8px 0;font-size:13px"></div>
   <div id="codeout"></div>
   <button class="btn" id="codeapply" hidden>Apply ticked changes</button>
  </div>
 </div>
</dialog>
<dialog id="autodlg" style="max-width:760px"><h2>Computer helper</h2>
 <p class="hint">Atlas can move your mouse, click, scroll, type and press keys for you. You always see the steps first, and nothing runs until you click Run. To stop at any time, click Stop or push the mouse into the top-left corner of the screen. On a Mac you may need to allow Accessibility for Atlas in System Settings. Windows blocks control of programs running as administrator.</p>
 <div id="autoinfo" class="hint" role="status"></div>
 <label for="autotask" style="margin-top:8px">What should it do?</label>
 <textarea id="autotask" rows="3" placeholder="e.g. Scroll down slowly, 10 times, waiting 2 seconds between each"></textarea>
 <label style="margin-top:6px"><input type="checkbox" id="autoshot" style="width:auto"> Let the AI look at my screen first (needs a vision model, experimental)</label>
 <button class="btn" id="automake">Make steps</button><button class="btn ghost" id="autopos">Where is my mouse?</button>
 <div id="autoposout" class="hint" role="status"></div>
 <div id="autoplan" hidden>
  <strong id="autosum"></strong>
  <ol id="autolines"></ol>
  <details><summary>Advanced: edit the steps</summary><textarea id="autojson" rows="8" spellcheck="false" style="font:12px ui-monospace,Consolas,monospace"></textarea><button class="btn ghost" id="autocheck">Check my edits</button></details>
  <label for="autodelay" style="margin-top:8px">Start after</label><select id="autodelay"><option value="5">5 seconds</option><option value="10" selected>10 seconds</option><option value="20">20 seconds</option></select>
  <button class="btn" id="autorun">Run these steps</button>
 </div>
 <div id="autostat" role="status" style="margin:10px 0;font-weight:600"></div>
 <button class="btn" id="autostop" hidden style="background:#c0392b;color:#fff">Stop</button><button class="btn ghost" id="autoclose">Close</button>
</dialog>
<dialog id="cyberdlg" style="max-width:900px"><h2>Cyber Trainer</h2>
 <div id="cyhome">
  <p class="hint">Short lessons with instant feedback, like a language app but for cybersecurity. Tap Ask Atlas any time you're stuck on a question &mdash; it knows exactly what you're looking at.</p>
  <div id="cytracks"></div>
  <div class="cysub">
   <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
    <div><b>Subnetting Drill</b><div class="hint" style="margin:2px 0 0">Unlimited random practice. Good for cramming before your final.</div></div>
    <select id="cydiff"><option value="easy">Easy (/24&ndash;/28)</option><option value="med" selected>Medium (/16&ndash;/30)</option><option value="hard">Hard (mixed, with VLSM)</option></select>
   </div>
   <div class="cystats" id="cystats"></div>
   <button class="btn" id="cydrill">Start drilling</button>
  </div>
 </div>
 <div id="cyplay" hidden>
  <div class="cybar"><button class="btn ghost" id="cyback">&larr; Back</button><div class="cyprog"><div id="cyprogfill"></div></div><div id="cyscore" class="hint"></div></div>
  <div id="cycard"></div>
 </div>
 <button class="btn ghost" id="cyclose" style="margin-top:14px">Close</button>
</dialog>
<dialog id="task"><h2>Organize a folder</h2>
 <p>Sorts files into folders by type, tucks existing folders into one called Folders, and moves old or probably useless files (old installers, empty files, duplicate downloads) into Review before deleting. Nothing is deleted, nothing moves until you approve the list, and you can undo it.</p>
 <label for="folder">Folder</label><input id="folder" value="~/Downloads">
 <label for="days" style="margin-top:8px">Count other files as old after this many days</label><input id="days" type="number" min="1" value="90">
 <button class="btn" id="prev">Preview</button>
 <div id="plan"></div>
 <button class="btn" id="go" hidden>Move these files</button><button class="btn ghost" id="undo">Undo last run</button><button class="btn ghost" id="close">Close</button>
</dialog>
<script>
const PRESETS=[
 {name:"Helpful all-rounder",text:"You are Atlas, a friendly, capable personal assistant. Be clear and practical. Keep answers short unless I ask for detail."},
 {name:"Coding partner",text:"You are a patient senior programmer. Explain your reasoning briefly, give working code in fenced code blocks, and point out likely mistakes."},
 {name:"Short and blunt",text:"Answer in as few words as possible. No filler, no pleasantries."},
 {name:"Study buddy",text:"You are a study buddy. Explain ideas simply with examples, then ask me one question to check I understood."},
 {name:"Game master",text:"You are a creative game master running a text adventure. Describe scenes vividly, offer choices, and keep track of my inventory and health."}
];
const $=id=>document.getElementById(id);
let S={chats:[],cur:null,model:"",system:PRESETS[0].text,preset:0,mode:"chat",game:"",notes:""};
let D=[],P=[],MODELS=[],VF=[],CF=[],VISION=[],PA={imgs:[],notes:[],labels:[]};
const VRX=/llava|vision|vl\b|vl:|minicpm-v|moondream|gemma3|bakllava|qwen2\.5vl|qwen3-vl/i;
const H={"X-Token":"__TOKEN__"};
let busy=false,ctl=null,saveT=null;

function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function md(t){
 return t.split("```").map((p,i)=>{
  if(i%2){const n=p.indexOf("\n");const c=n>=0?p.slice(n+1):p;return "<pre><code>"+esc(c.replace(/\n$/,""))+"</code></pre>"}
  return esc(p).replace(/`([^`\n]+)`/g,"<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g,"<strong>$1</strong>").replace(/\n/g,"<br>");
 }).join("");
}
function cur(){return S.chats.find(c=>c.id===S.cur)}
const fixErr=m=>/unknown model architecture|mllama/i.test(m||"")?"The model you picked needs a newer version of Ollama than the one installed. Update Ollama (it is free and keeps your downloaded models): open Settings and click Download latest Ollama, or turn on Update Ollama automatically. Or choose a different model that can see images, for example run  ollama pull gemma3:4b  in a terminal.":m;
function status(t,ok){t=fixErr(t);$("status").textContent=t||"";$("status").classList.toggle("ok",!!ok);document.querySelectorAll(".smsg").forEach(e=>{e.textContent=t||""})}
function chip(){$("modelchip").textContent=(S.model||"No model")+" \u00b7 "+(S.mode==="game"?"Game helper":"Chat")}
function save(){
 clearTimeout(saveT);
 saveT=setTimeout(()=>fetch("/api/state",{method:"POST",headers:H,body:JSON.stringify(S)}).catch(()=>{}),300);
}
function newChat(){
 const c={id:String(Date.now()),title:"New chat",messages:[]};
 S.chats.unshift(c);S.cur=c.id;save();render();$("input").focus();return c;
}
let Q="";
function chatItem(c){
 const d=document.createElement("div");d.className="chat"+(c.id===S.cur?" on":"");
 const t=document.createElement("button");t.className="t";t.textContent=c.title;
 t.onclick=()=>{S.cur=c.id;document.body.classList.remove("open");save();render()};
 const e=document.createElement("button");e.className="x";e.textContent="\u270e";e.setAttribute("aria-label","Change topic");
 e.onclick=()=>{const v=prompt("Topic for this chat",c.topic||"");if(v&&v.trim()){c.topic=v.trim();save();renderChats()}};
 const x=document.createElement("button");x.className="x";x.textContent="\u00d7";x.setAttribute("aria-label","Delete chat");
 x.onclick=()=>{S.chats=S.chats.filter(k=>k.id!==c.id);if(S.cur===c.id)S.cur=S.chats[0]?S.chats[0].id:null;save();render()};
 d.append(t,e,x);return d;
}
function renderChats(){
 const L=$("chats"),q=Q.toLowerCase(),g={};L.innerHTML="";
 S.chats.forEach(c=>{
  if(q&&!(c.title+" "+(c.topic||"")+" "+c.messages.map(m=>m.content).join(" ")).toLowerCase().includes(q))return;
  (g[c.topic||"Unsorted"]=g[c.topic||"Unsorted"]||[]).push(c);
 });
 Object.keys(g).sort((a,b)=>a==="Unsorted"?1:b==="Unsorted"?-1:a.localeCompare(b)).forEach(k=>{
  const det=document.createElement("details"),sm=document.createElement("summary");
  sm.textContent=k+" ("+g[k].length+")";det.append(sm);
  det.open=!!q||!(S.closed&&S.closed[k]);
  det.ontoggle=()=>{if(!Q){S.closed=S.closed||{};S.closed[k]=!det.open;save()}};
  g[k].forEach(c=>det.append(chatItem(c)));L.append(det);
 });
}
function renderMsgs(){
 const c=cur(),L=$("list");L.innerHTML="";
 if(!c||!c.messages.length){
  L.innerHTML='<div class="empty"><h2>What should we work on?</h2><p>Everything runs on your computer through Ollama. Nothing is sent online.</p><div class="chips"></div></div>';
  (S.mode==="game"?["Plan a build for my character","What should I do next?","Explain the lore of this game"]:["Plan my week","Explain how the internet works, simply","Help me fix a bug in my code"]).forEach(s=>{
   const b=document.createElement("button");b.className="btn ghost";b.textContent=s;b.onclick=()=>send(s);L.querySelector(".chips").append(b);
  });
  return;
 }
 c.messages.forEach(m=>{
  const d=document.createElement("div");d.className="m "+m.role;
  if(m.role==="user"){d.textContent=m.content;thumbs(m,d)}else d.innerHTML=m.content?md(m.content):"<em>Thinking\u2026 (the first reply after a pause loads the model)</em>";
  if(m.role==="assistant"&&m.content&&!busy)d.append(actions(m,d));
  L.append(d);
 });
 $("msgs").scrollTop=$("msgs").scrollHeight;
}
function render(){renderChats();renderMsgs();chip();$("send").textContent=busy?"Stop":"Send"}

async function loadModels(){
 try{
  const r=await fetch("/api/models");const j=await r.json();
  if(!r.ok)throw new Error(j.error||"Error");
  const names=(j.models||[]).map(m=>m.name);MODELS=names;
  try{const c=await(await fetch("/api/caps")).json();VISION=names.filter(n=>c[n]&&c[n].length?c[n].includes("vision"):VRX.test(n))}catch(e){VISION=names.filter(n=>VRX.test(n))}
  const sel=$("model");sel.innerHTML="";
  names.forEach(n=>{const o=document.createElement("option");o.value=n;o.textContent=n;sel.append(o)});
  if(!names.length){status("No models installed yet. In a terminal run:  ollama pull llama3.2");S.model="";return}
  if(!names.includes(S.model))S.model=names[0];
  sel.value=S.model;status("");chip();
 }catch(e){status(e.message)}
}

async function api(p,body,name){
 const r=await fetch(p,{method:"POST",headers:name?{...H,"X-Name":encodeURIComponent(name)}:H,body:name?body:JSON.stringify(body||{})});
 const j=await r.json();if(!r.ok)throw new Error(j.error||"Something went wrong");return j;
}
function sys(){
 return (V?"This is a spoken conversation: answer in one to three short sentences of plain speech, with no lists, markdown or code. ":"")+sys0();
}
function sys0(){
 if(S.mode!=="game")return S.system;
 return "You are an expert guide for the video game "+(S.game||"the player's game")+". Help with build planning, strategy and lore. Never invent stats, item names or quests: if you are not sure, say so and say what to check. Ask which patch or version if numbers matter. Prefer the reference material when it is provided.\n"+(S.notes?"The player's character or build: "+S.notes+"\n":"")+"Style: "+S.system;
}
const STOP=/\b(the|and|for|you|are|what|how|can|with|that|this|from|have|about|does|should|which|when|where)\b/g;
function retrieve(q){
 const w=[...new Set(q.toLowerCase().replace(STOP," ").match(/[a-z0-9']{3,}/g)||[])],hits=[];
 D.filter(d=>d.on).forEach(d=>{
  for(let i=0;i<d.text.length;i+=700){
   const c=d.text.slice(i,i+900),l=c.toLowerCase();let sc=0;
   w.forEach(x=>{const n=l.split(x).length-1;sc+=n?1+Math.log(n):0});
   if(sc)hits.push([sc,d.name,c]);
  }
 });
 return hits.sort((a,b)=>b[0]-a[0]).slice(0,4);
}
function renderDocs(){
 $("docs").innerHTML="";
 D.forEach(d=>{
  const r=document.createElement("div");r.className="doc";
  const c=document.createElement("input");c.type="checkbox";c.checked=d.on;c.style.width="auto";
  c.onchange=()=>{d.on=c.checked;saveDocs()};
  const n=document.createElement("span");n.textContent=d.name;n.title=d.name;
  const x=document.createElement("button");x.className="x";x.textContent="\u00d7";x.setAttribute("aria-label","Remove file");
  x.style.cssText="background:none;border:0;color:var(--mute)";
  x.onclick=()=>{D=D.filter(k=>k!==d);saveDocs();renderDocs()};
  r.append(c,n,x);$("docs").append(r);
 });
}
function saveDocs(){api("/api/docs",D).catch(()=>{})}
async function addFiles(files){
 for(const f of files){
  try{
   status("Reading "+f.name+"\u2026",true);
   const j=await api("/api/extract",await f.arrayBuffer(),f.name);
   D.push({name:f.name,text:j.text,on:true});
  }catch(e){status(e.message);return}
 }
 status("");saveDocs();renderDocs();
}
function openTask(f){$("folder").value=f;$("plan").textContent="";$("go").hidden=true;$("task").showModal()}

const CODER=/coder|codellama|codegemma|starcoder/i;
function mkModel(){return MODELS.find(n=>CODER.test(n))||S.model}
const GP="You are an expert JavaScript game programmer. Write ONE complete HTML file with the HTML, CSS and JavaScript together: no libraries, no images, no network, no alert() or prompt(). Rules: use a single canvas and a requestAnimationFrame loop; read the keyboard with keydown and keyup flags; draw with fillRect, arc and fillText; show the score; show Game Over and restart on Space or R; call preventDefault for arrow keys and Space; define every variable before you use it; keep it under 200 lines, simple and correct rather than fancy. You are given a WORKING game. Change it to do what is asked, keep the parts that already work, and never start over. Reply with the whole file in one ```html code block, then one short sentence about what you changed.";
const STARTER=`<!doctype html><html><body style="margin:0;background:#111;display:flex;justify-content:center;align-items:center;height:100vh"><canvas id="c" width="480" height="360"></canvas><script>
const c=document.getElementById("c"),x=c.getContext("2d"),keys={};
let p,blocks,score,over,t;
function reset(){p={x:220,y:320,w:40,h:20,s:5};blocks=[];score=0;over=false;t=0}
reset();
addEventListener("keydown",e=>{keys[e.code]=true;if(["Space","ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.code))e.preventDefault();if(over&&(e.code==="Space"||e.code==="KeyR"))reset()});
addEventListener("keyup",e=>{keys[e.code]=false});
function update(){
 if(over)return;t++;
 if(keys.ArrowLeft||keys.KeyA)p.x-=p.s;
 if(keys.ArrowRight||keys.KeyD)p.x+=p.s;
 p.x=Math.max(0,Math.min(c.width-p.w,p.x));
 if(t%30===0)blocks.push({x:Math.random()*440,y:-20,w:30,h:30,s:2+Math.random()*2});
 blocks.forEach(b=>{b.y+=b.s;if(b.x<p.x+p.w&&b.x+b.w>p.x&&b.y<p.y+p.h&&b.y+b.h>p.y)over=true});
 blocks=blocks.filter(b=>{if(b.y>c.height){score++;return false}return true});
}
function draw(){
 x.fillStyle="#111";x.fillRect(0,0,c.width,c.height);
 x.fillStyle="#6cf";x.fillRect(p.x,p.y,p.w,p.h);
 x.fillStyle="#f66";blocks.forEach(b=>x.fillRect(b.x,b.y,b.w,b.h));
 x.fillStyle="#fff";x.font="16px sans-serif";x.fillText("Score: "+score,10,22);
 if(over){x.font="28px sans-serif";x.fillText("Game over, press Space",110,180)}
}
function loop(){update();draw();requestAnimationFrame(loop)}
loop();
<\/script></body></html>`;
let gbusy=false,gctl=null;
function MK(){return S.maker||(S.maker={code:"",ver:[],msgs:[]})}
function mkShow(){
 const m=MK();
 $("gframe").srcdoc=m.code?'<script>onerror=function(a,b,c){parent.postMessage({gameError:a+" (line "+c+")"},"*")}<\/script>'+m.code:"";
 $("gcode").value=m.code;$("gempty").hidden=!!m.code;$("gerr").hidden=true;
}
function mkMsgs(){
 const L=$("gmsgs"),m=MK();L.innerHTML="";
 if(!m.msgs.length){
  L.innerHTML='<p class="hint">Describe a game and it appears on the right. Then ask for changes, like "make the player faster" or "add a score". Click the game once to control it with your keyboard.</p><p class="hint">Model: '+esc(mkModel())+(CODER.test(mkModel())?'':'. For much better games, run <code>ollama pull qwen2.5-coder:7b</code> in a terminal, then reopen this window.')+'</p><div class="chips" style="justify-content:flex-start"></div>';
  ["A snake game","A space shooter","A cube that jumps over spikes"].forEach(t=>{const b=document.createElement("button");b.className="btn ghost";b.textContent=t;b.onclick=()=>mkSend(t);L.querySelector(".chips").append(b)});
 }
 m.msgs.forEach(x=>{const d=document.createElement("div");d.className="m "+x.role;d.textContent=x.text;L.append(d)});
 L.scrollTop=L.scrollHeight;
}
let gfixes=0;
async function mkSend(text,auto){
 if(gbusy){gctl&&gctl.abort();return}
 if(!auto)gfixes=0;
 text=(text||"").trim();if(!text)return;
 if(!S.model){await loadModels();if(!S.model)return}
 const m=MK(),a={role:"assistant",text:"Writing code\u2026"};
 m.msgs.push({role:"user",text},a);$("ginput").value="";mkMsgs();
 gbusy=true;gctl=new AbortController();$("gsend").textContent="Stop";
 const prompt=m.code?"Current game:\n```html\n"+m.code+"\n```\nMake ONLY this change and return the whole file: "+text:"Here is a WORKING starter game:\n```html\n"+STARTER+"\n```\nChange it into this game, keeping the same structure (reset, update, draw, loop): "+text;
 let out="",err="";
 try{
  const r=await fetch("/api/chat",{method:"POST",headers:H,signal:gctl.signal,body:JSON.stringify({model:mkModel(),stream:true,keep_alive:"30m",options:{num_ctx:16384,temperature:0.2},messages:[{role:"system",content:GP},{role:"user",content:prompt}]})});
  if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||"Ollama returned error "+r.status)}
  const rd=r.body.getReader(),dec=new TextDecoder();let buf="";
  while(true){
   const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf("\n"))>=0){
    const line=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!line)continue;
    const j=JSON.parse(line);if(j.error)throw new Error(j.error);
    if(j.message){out+=j.message.content;$("gmsgs").lastElementChild.textContent="Writing code\u2026 "+out.length+" characters"}
   }
  }
 }catch(e){err=e.name==="AbortError"?"Stopped.":e.message}
 const mm=out.match(/```(?:html)?\s*\n([\s\S]*?)(```|$)/);
 const code=mm?mm[1]:(/<html|<!doctype/i.test(out)?out:"");
 if(code.trim().length>40){
  m.ver.push(m.code);if(m.ver.length>8)m.ver.shift();m.code=code.trim();
  a.text=out.replace(/```[\s\S]*?(```|$)/,"").trim()||"Done. Ask for any change.";mkShow();
 }else a.text=fixErr(err)||"The model did not return a game. Try again, or describe it differently.";
 gbusy=false;gctl=null;$("gsend").textContent="Send";mkMsgs();save();
}
async function gen(messages,onText,opts){
 ctl=new AbortController();
 const r=await fetch("/api/chat",{method:"POST",headers:H,signal:ctl.signal,body:JSON.stringify({model:(opts&&opts.model)||S.model,stream:true,keep_alive:"30m",options:Object.assign({num_ctx:8192},opts,{model:undefined}),messages})});
 if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||"Ollama returned error "+r.status)}
 const rd=r.body.getReader(),dec=new TextDecoder();let buf="",out="";
 while(true){
  const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
  while((i=buf.indexOf("\n"))>=0){const l=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!l)continue;const j=JSON.parse(l);if(j.error)throw new Error(j.error);if(j.message){out+=j.message.content;onText(out)}}
 }
 return out;
}
async function humanize(m,d){
 if(busy)return;
 const mine=(S.voiceSample||"")+"\n"+S.chats.flatMap(c=>c.messages).filter(x=>x.role==="user"&&x.content.length>25).slice(-8).map(x=>x.content).join("\n");
 const sysmsg="Rewrite the text so it sounds like a real person wrote it, in the voice of the writing samples. Match their word choice, sentence length, punctuation and level of casualness. Keep every fact and the full meaning. Use plain words and contractions, and vary the sentence length naturally. Avoid stock phrases like delve, moreover, in conclusion, and it is important to note. Do not add headings or lists unless the original has them. Output only the rewritten text.";
 busy=true;ctl=null;$("send").textContent="Stop";status("Rewriting in your voice\u2026",true);
 let out="";
 try{out=await gen([{role:"system",content:sysmsg},{role:"user",content:"Writing samples from me:\n"+(mine.trim()||"(none yet, so keep it casual and plain)")+"\n\nText to rewrite:\n"+m.content}],t=>{d.innerHTML=md(t)},{temperature:0.8})}
 catch(e){if(e.name!=="AbortError")status(e.message)}
 if(out.trim().length>20){m.orig=m.content;m.content=out.trim();m.on=true;status("")}
 else status("");
 busy=false;ctl=null;save();render();
}

const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let V=false,rec=null;
function vbtnState(on){
 const b=$("vbtn"),t=on?"Stop voice mode":"Voice mode";
 b.classList.toggle("on",on);b.setAttribute("aria-pressed",on);b.title=t;b.setAttribute("aria-label",t);
}
function stopVoice(){V=false;try{rec&&rec.abort()}catch(e){}speechSynthesis.cancel();vbtnState(false);status("")}
function speak(t,done){
 if(!window.speechSynthesis){status("Your browser can't read aloud.");if(done)done();return}
 speechSynthesis.cancel();
 const clean=t.replace(/```[\s\S]*?```/g," (code left out) ").replace(/[*_`#>]/g,"");
 const parts=clean.match(/[^.!?\n]+[.!?]*\s*/g)||[clean];
 let i=0;
 const next=()=>{
  if(i>=parts.length){if(done)done();return}
  const u=new SpeechSynthesisUtterance(parts[i++]),v=speechSynthesis.getVoices().find(x=>x.name===S.voice);
  if(v)u.voice=v;u.rate=S.rate||1;u.pitch=S.pitch||1;
  u.onend=next;u.onerror=()=>{i=parts.length;if(done)done()};
  speechSynthesis.speak(u);
 };
 next();
}
function listen(){
 if(!V)return;
 rec=new SR();rec.lang=navigator.language||"en-US";rec.interimResults=true;
 let said="";
 rec.onresult=e=>{said=[...e.results].map(r=>r[0].transcript).join("");$("input").value=said;autosize()};
 rec.onerror=e=>{if(e.error!=="no-speech"&&e.error!=="aborted"){stopVoice();status(e.error==="not-allowed"?"The microphone is blocked. Allow it in the address bar, then try again.":"Voice input failed ("+e.error+"). It needs Chrome or Edge and an internet connection.")}};
 rec.onend=async()=>{
  if(!V)return;
  if(!said.trim())return listen();
  await send(said);
  const c=cur(),l=c&&c.messages[c.messages.length-1];
  if(V&&l&&l.role==="assistant"&&l.content){status("Speaking\u2026 click the microphone to stop",true);speak(l.content,listen)}else listen();
 };
 status("Listening\u2026",true);rec.start();
}

async function ask(messages,opts,model){
 const r=await fetch("/api/chat",{method:"POST",headers:H,body:JSON.stringify({model:model||S.model,stream:false,keep_alive:"30m",options:Object.assign({temperature:0,num_ctx:4096},opts),messages})});
 const j=await r.json();if(!r.ok)throw new Error(j.error||"error");
 return ((j.message&&j.message.content)||"").trim();
}
async function sortChat(c){
 try{
  const first=c.messages.filter(m=>m.content).slice(0,2).map(m=>m.role+": "+m.content.slice(0,300)).join("\n");
  const known=[...new Set(S.chats.map(x=>x.topic).filter(x=>x&&x!=="General"))].join(", ")||"none yet";
  const t=await ask([{role:"system",content:"You file chats into topics. Reply with exactly: Topic | Title. The topic is 1 to 3 words. Reuse an existing topic when it fits, otherwise invent a short new one. The title is 2 to 6 words describing this chat. No other text."},{role:"user",content:"Existing topics: "+known+"\n\nChat:\n"+first}],{num_predict:30});
  const [tp,ti]=t.split("|").map(x=>x.replace(/["*.\n]/g,"").trim());
  if(tp&&tp.length<30){c.topic=tp;if(ti&&ti.length<60)c.title=ti}
 }catch(e){}
 c.topic=c.topic||"General";
}

function remember(id,key){
 const el=$(id),box=el.type==="checkbox";
 if(S[key]!==undefined){if(box)el.checked=S[key];else el.value=S[key]}
 el.addEventListener("change",()=>{S[key]=box?el.checked:el.value;save()});
 if(!box)el.addEventListener("input",()=>{S[key]=el.value;save()});
}
function vsay(t){$("vmsg").textContent=fixErr(t)||""}
const vsrc=v=>"/api/file?t="+H["X-Token"]+"&p="+encodeURIComponent(v.path);
function vcount(){vsay(VF.length+" videos, "+VF.filter(v=>v.sel).length+" ticked.")}
function vRender(){
 const g=$("vgrid");g.innerHTML="";
 const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.src=e.target.dataset.src;io.unobserve(e.target)}}),{root:g});
 VF.forEach((v,idx)=>{
  const c=document.createElement("div");c.className="vc";
  v.el=c;c.draggable=true;c.addEventListener("dragstart",e=>vDrag(e,v));c.addEventListener("dragend",vDragEnd);
  const isImg=/\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif)$/i.test(v.name),vid=document.createElement(isImg?"img":"video");
  if(isImg){vid.alt=v.name;vid.style.cssText="width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px"}else{vid.controls=true;vid.muted=true;vid.preload="metadata"}
  vid.dataset.src=vsrc(v)+(isImg?"":"#t=1");
  vid.draggable=false;if(!isImg)vid.addEventListener("mousedown",()=>{c.draggable=false});
  if(!isImg)vid.setAttribute("controlsList","nofullscreen");
  vid.ondblclick=()=>vvOpen(idx);
  if(isImg){vid.style.cursor="zoom-in";vid.onclick=()=>vvOpen(idx)}
  const lab=document.createElement("label"),cb=document.createElement("input"),t=document.createElement("span");
  lab.style.cssText="display:flex;gap:6px;align-items:flex-start;margin:4px 0;color:var(--ink)";
  cb.type="checkbox";cb.checked=!!v.sel;cb.style.width="auto";cb.onchange=()=>{v.sel=cb.checked;vmark(v);vcount()};v.cb=cb;vmark(v);
  t.textContent=v.name+" ("+v.mb+" MB)";lab.append(cb,t);
  const ai=document.createElement("button");ai.className="btn ghost";ai.style.cssText="padding:3px 8px;font-size:12px";ai.textContent="Ask AI about this";
  ai.onclick=async()=>{
   try{if(isImg){PA.imgs.push(await shrink(await(await fetch(vsrc(v))).blob()))}else{const n=await watch(vsrc(v),v.name,vsay);PA.notes.push(n);PA.labels.push(v.name)}renderPA();$("vdlg").close();$("input").focus();status("Video notes attached. Ask your question.",true)}
   catch(e){vsay(e.message)}
  };
  const wrap=document.createElement("div"),ex=document.createElement("button");
  wrap.className="vwrap";ex.className="vexp";ex.title="Full screen";ex.setAttribute("aria-label","Full screen: "+v.name);ex.innerHTML=ICON.expand;ex.onclick=()=>vvOpen(idx);
  const grip=document.createElement("div");grip.className="vgrip";grip.title="Drag to a folder";grip.innerHTML=ICON.grip;
  const row=document.createElement("div"),rn=document.createElement("button");
  row.className="vname";rn.className="vren";rn.title="Rename";rn.setAttribute("aria-label","Rename "+v.name);rn.innerHTML=ICON.pencil;rn.onclick=()=>vRename(v,t);
  row.append(lab,rn);wrap.append(vid,ex,grip);c.append(wrap,row,ai);g.append(c);io.observe(vid);
 });
}
function unloadPreview(v){const x=v.el&&v.el.querySelector("video");if(x){x.pause();x.removeAttribute("src");x.load()}}
async function doRename(v,newBase){
 unloadPreview(v);await new Promise(r=>setTimeout(r,150));  // let go of the file first
 const j=await api("/api/renamefile",{path:v.path,new:newBase});
 v.name=j.name;v.path=j.path;vRender();
 vsay('Renamed to "'+j.name+'". Undo last run puts the old name back.');
}
function vRename(v,t){
 const dot=v.name.lastIndexOf("."),ext=v.name.slice(dot),base=v.name.slice(0,dot);
 const i=document.createElement("input"),x=document.createElement("span");
 i.type="text";i.className="vedit";i.value=base;i.maxLength=120;i.setAttribute("aria-label","New name for "+v.name);x.textContent=ext;
 t.textContent="";t.append(i,x);i.focus();i.select();
 i.addEventListener("mousedown",()=>{if(v.el)v.el.draggable=false});
 let done=false;
 const finish=async save=>{
  if(done)return;done=true;
  const nb=i.value.trim();
  if(!save||!nb||nb===base){t.textContent=v.name+" ("+v.mb+" MB)";return}
  try{await doRename(v,nb)}catch(e){vsay(e.message);t.textContent=v.name+" ("+v.mb+" MB)"}
 };
 i.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();finish(true)}else if(e.key==="Escape"){e.preventDefault();e.stopPropagation();finish(false)}};
 i.onblur=()=>finish(true);
}
async function vvRename(){
 const v=VF[VI];if(!v)return;
 const nb=$("vvrname").value.trim();if(!nb){$("vvrnmsg").textContent="Type a name first.";return}
 try{vvStop();await doRename(v,nb);$("vvrn").hidden=true;vvShow(VI)}
 catch(e){$("vvrnmsg").textContent=e.message;vvShow(VI)}
}
let VFD=[],VUP="",VCUR="",FSEL=new Set(),FNEW=false,dragFiles=[];
function vmark(v){if(v.cb)v.cb.checked=!!v.sel;if(v.el)v.el.classList.toggle("sel",!!v.sel)}
function vDrag(e,v){
 const set=v.sel?VF.filter(x=>x.sel):[v];
 dragFiles=set.map(x=>x.path);
 e.dataTransfer.effectAllowed="move";e.dataTransfer.setData("text/plain",set.length+" files");
 const g=document.createElement("div");g.className="dragghost";g.textContent=set.length>1?set.length+" files":v.name;
 $("vdlg").append(g);e.dataTransfer.setDragImage(g,12,12);setTimeout(()=>g.remove(),0);
}
function vDragEnd(){dragFiles=[];document.querySelectorAll(".fcard.drop").forEach(x=>x.classList.remove("drop"))}
document.addEventListener("mouseup",()=>{VF.forEach(x=>{if(x.el)x.el.draggable=true})});
async function vLoad(final){
 try{
  vsay("Loading\u2026");VCUR=$("vfolder").value;
  const j=await api("/api/videos",{folder:VCUR,deep:$("vdeep").checked});
  VF=j.videos;VFD=j.folders||[];VUP=j.parent||"";FNEW=false;FSEL.clear();fRender();vRender();
  vsay(final||(VF.length?VF.length+" files. Tick the ones you want, or drag them onto a folder.":"No video or image files here. Try another folder, or tick the subfolders box."));
 }catch(e){vsay(e.message)}
}
async function vFolders(){const j=await api("/api/videos",{folder:VCUR,deep:$("vdeep").checked});VFD=j.folders||[];VUP=j.parent||"";fRender()}
function openFolder(path){$("vfolder").value=path;S.vfolder=path;save();$("vload").click()}
async function moveInto(f,files){
 try{
  vsay("Moving "+files.length+" file"+(files.length>1?"s":"")+"\u2026");
  const j=await api("/api/send",{files,dest:f.path,mode:"move"});
  await vLoad("Moved "+j.n+" file"+(j.n===1?"":"s")+" into "+f.name+". Undo last run puts them back.");
 }catch(e){vsay(e.message)}
}
async function makeFolder(name){
 try{const j=await api("/api/folder",{action:"create",folder:VCUR||$("vfolder").value,name});FNEW=false;await vFolders();vsay('Created the folder "'+j.name+'". Drag files onto it.')}
 catch(e){vsay(e.message)}
}
function fMenu(f,btn){
 const m=$("fmenu");m.innerHTML="";
 [["Open",()=>openFolder(f.path)],["Rename",async()=>{
   const n=prompt("New name for this folder",f.name);if(!n||n.trim()===f.name)return;
   try{await api("/api/folder",{action:"rename",folder:VCUR,name:f.name,new:n});await vLoad("Renamed.")}catch(e){vsay(e.message)}
  }],["Delete (only if empty)",async()=>{
   try{await api("/api/folder",{action:"delete",folder:VCUR,name:f.name});await vLoad('Deleted the empty folder "'+f.name+'".')}catch(e){vsay(e.message)}
  }]].forEach(([t,fn])=>{const b=document.createElement("button");b.textContent=t;b.onclick=()=>{m.hidden=true;fn()};m.append(b)});
 const r=btn.getBoundingClientRect();m.style.left=Math.max(8,Math.min(r.left-120,innerWidth-176))+"px";m.style.top=(r.bottom+4)+"px";m.hidden=false;m.firstChild.focus();
}
function fRender(){
 $("fsec").hidden=false;$("fup").hidden=!VUP;$("fdel").hidden=!FSEL.size;
 const g=$("fgrid");g.innerHTML="";
 if(FNEW){
  const c=document.createElement("div"),ic=document.createElement("span"),i=document.createElement("input"),ok=document.createElement("button");
  c.className="fcard";ic.innerHTML=ICON.folder;i.className="fedit";i.type="text";i.maxLength=80;i.placeholder="Folder name";i.setAttribute("aria-label","New folder name");
  ok.className="fok";ok.setAttribute("aria-label","Create folder");ok.innerHTML=ICON.check;ok.onclick=()=>makeFolder(i.value);
  i.onkeydown=e=>{if(e.key==="Enter")makeFolder(i.value);else if(e.key==="Escape"){e.preventDefault();e.stopPropagation();FNEW=false;fRender()}};
  c.append(ic,i,ok);g.append(c);i.focus();
 }
 VFD.forEach(f=>{
  const c=document.createElement("div"),cb=document.createElement("input"),ic=document.createElement("span"),nm=document.createElement("div"),dots=document.createElement("button");
  c.className="fcard";c.tabIndex=0;c.title=f.name+" ("+f.count+" file"+(f.count===1?"":"s")+")";
  cb.type="checkbox";cb.checked=FSEL.has(f.name);cb.setAttribute("aria-label","Select folder "+f.name);
  cb.onclick=e=>e.stopPropagation();cb.onchange=()=>{if(cb.checked)FSEL.add(f.name);else FSEL.delete(f.name);$("fdel").hidden=!FSEL.size};
  ic.innerHTML=ICON.folder;nm.className="fname";nm.textContent=f.name;
  dots.className="fdots";dots.innerHTML=ICON.dots;dots.setAttribute("aria-label","Folder options for "+f.name);dots.onclick=e=>{e.stopPropagation();fMenu(f,dots)};
  c.onclick=()=>openFolder(f.path);c.onkeydown=e=>{if(e.key==="Enter"&&e.target===c)openFolder(f.path)};
  c.addEventListener("dragover",e=>{if(dragFiles.length){e.preventDefault();e.dataTransfer.dropEffect="move";c.classList.add("drop")}});
  c.addEventListener("dragleave",e=>{if(!c.contains(e.relatedTarget))c.classList.remove("drop")});
  c.addEventListener("drop",e=>{e.preventDefault();c.classList.remove("drop");if(dragFiles.length)moveInto(f,dragFiles.slice())});
  c.append(cb,ic,nm,dots);g.append(c);
 });
}
function renderPick(){
 const L=$("vvpl");L.innerHTML="";$("vvpmsg").textContent="";
 if(!VFD.length){L.textContent="No folders yet. Name one below.";L.style.cssText="font-size:13px;color:#bbb"}else L.style.cssText="";
 VFD.forEach(f=>{const b=document.createElement("button");b.textContent=f.name;b.onclick=()=>vvAddTo(f);L.append(b)});
}
async function vvAddTo(f){
 const v=VF[VI];if(!v)return;
 try{
  await api("/api/send",{files:[v.path],dest:f.path,mode:"move"});
  $("vvpick").hidden=true;VF.splice(VI,1);vRender();vFolders().catch(()=>{});
  if(!VF.length){vvClose();vsay("Moved the last file into "+f.name+".");return}
  vvShow(VI);vsay("Moved "+v.name+" into "+f.name+". Undo last run puts it back.");
 }catch(e){$("vvpmsg").textContent=e.message}
}
const IMGRX=/\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif)$/i;
let VI=-1;
function vvStop(){const st=$("vvstage");st.querySelectorAll("video").forEach(x=>{x.pause();x.removeAttribute("src");x.load()});st.innerHTML=""}
function vvShow(i){
 if(!VF.length)return;
 $("vvpick").hidden=true;$("vvrn").hidden=true;
 VI=(i+VF.length)%VF.length;
 const v=VF[VI],img=IMGRX.test(v.name);vvStop();
 const el=document.createElement(img?"img":"video");el.src=vsrc(v);
 if(img)el.alt=v.name;else{el.controls=true;el.setAttribute("controlsList","nofullscreen");el.autoplay=true}
 $("vvstage").append(el);if(!img)el.play().catch(()=>{});
 $("vvcb").checked=!!v.sel;
 $("vvtitle").textContent=v.name+" ("+(VI+1)+" of "+VF.length+")\n\u2190 \u2192 move   S select   Esc close";
}
function vvOpen(i){
 $("vview").hidden=false;vvShow(i);$("vview").focus();
 const r=$("vview").requestFullscreen&&$("vview").requestFullscreen();if(r&&r.catch)r.catch(()=>{});
}
function vvHide(){vvStop();$("vview").hidden=true;vcount()}
function vvClose(){vvHide();if(document.fullscreenElement)document.exitFullscreen().catch(()=>{})}
function vvToggle(){const v=VF[VI];if(!v)return;v.sel=!v.sel;$("vvcb").checked=v.sel;vmark(v)}
function csay(t){$("cmsg").textContent=fixErr(t)||""}
function ccount(){csay(CF.length+" files, "+CF.filter(v=>v.sel).length+" ticked.")}
function cRender(){
 const g=$("clist");g.innerHTML="";
 CF.forEach(v=>{
  const lab=document.createElement("label"),cb=document.createElement("input"),t=document.createElement("span");
  lab.className="crow";cb.type="checkbox";cb.checked=!!v.sel;cb.onchange=()=>{v.sel=cb.checked;ccount()};
  t.textContent=v.name+" ("+v.mb+" MB)";lab.append(cb,t);g.append(lab);
 });
}
function openConvert(){
 if(!S.cfolder&&S.vfolder)$("cfolder").value=S.vfolder;
 $("cvdlg").showModal();
 if((S.cfolder||S.vfolder)&&!CF.length)$("cload").click();
}
function openVideos(){$("vdlg").showModal();if(S.vfolder&&!VF.length)$("vload").click()}
function openGH(){api("/api/ghtoken",{}).then(j=>{$("ghhas").textContent=j.saved?"(saved)":"(none saved yet)"}).catch(()=>{});$("ghdlg").showModal()}

const NOVISION="None of your installed models can look at images. In a terminal run: ollama pull gemma3:4b   (or llama3.2-vision, or qwen2.5vl:3b), then reload this page.";
const isVision=n=>VISION.includes(n);
const visionModel=()=>isVision(S.model)?S.model:(VISION.find(n=>!/llama3\.2-vision|mllama/i.test(n))||VISION[0]);
const mc=m=>m.ctx?m.content+"\n\n"+m.ctx:m.content;
const fmt=t=>Math.floor(t/60)+":"+String(Math.floor(t%60)).padStart(2,"0");
function thumbs(m,d){
 (m.images||[]).forEach(b=>{const im=document.createElement("img");im.src="data:image/jpeg;base64,"+b;im.alt="Attached image";im.style.cssText="display:block;max-height:140px;max-width:100%;border-radius:8px;margin-top:6px";d.append(im)});
 if(m.ctx){const n=document.createElement("div");n.style.cssText="font-size:12px;color:var(--mute);margin-top:4px";n.textContent="Video attached";d.append(n)}
}
function renderPA(){
 const b=$("pend");b.innerHTML="";
 const chip=(el,rm)=>{
  const w=document.createElement("div"),x=document.createElement("button");
  w.style.cssText="display:flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 8px;font-size:12px";
  x.textContent="\u00d7";x.setAttribute("aria-label","Remove attachment");x.style.cssText="background:none;border:0;color:var(--mute)";x.onclick=rm;
  w.append(el,x);b.append(w);
 };
 PA.imgs.forEach((g,i)=>{const im=document.createElement("img");im.src="data:image/jpeg;base64,"+g;im.style.cssText="height:40px;border-radius:4px";chip(im,()=>{PA.imgs.splice(i,1);renderPA()})});
 PA.notes.forEach((n,i)=>{const s=document.createElement("span");s.textContent="Video: "+PA.labels[i];chip(s,()=>{PA.notes.splice(i,1);PA.labels.splice(i,1);renderPA()})});
}
function shrink(file){
 return new Promise((res,rej)=>{
  const im=new Image(),u=URL.createObjectURL(file);
  im.onload=()=>{const sc=Math.min(1,896/Math.max(im.width,im.height)),cv=document.createElement("canvas");cv.width=im.width*sc;cv.height=im.height*sc;
   cv.getContext("2d").drawImage(im,0,0,cv.width,cv.height);URL.revokeObjectURL(u);res(cv.toDataURL("image/jpeg",0.82).split(",")[1])};
  im.onerror=()=>rej(new Error("Couldn't read that image."));im.src=u;
 });
}
function frames(src,n){
 return new Promise((res,rej)=>{
  const v=document.createElement("video");v.muted=true;v.preload="auto";v.src=src;
  const to=setTimeout(()=>rej(new Error("The video took too long to open. MP4 or WebM files work best in the browser.")),30000);
  v.onerror=()=>{clearTimeout(to);rej(new Error("Your browser can't open that video (some codecs like HEVC don't work). MP4 or WebM usually do."))};
  v.onloadedmetadata=async()=>{
   clearTimeout(to);
   if(!isFinite(v.duration)){  // recorded clips often lack a length, so make the browser work it out
    await new Promise(r=>{v.ontimeupdate=()=>{v.ontimeupdate=null;r()};v.currentTime=1e7;setTimeout(r,3000)});
   }
   if(!isFinite(v.duration)||!v.videoWidth){rej(new Error("Couldn't read that video."));return}
   const out=[],cv=document.createElement("canvas"),sc=Math.min(1,768/Math.max(v.videoWidth,v.videoHeight));
   cv.width=v.videoWidth*sc;cv.height=v.videoHeight*sc;
   for(let i=0;i<n;i++){
    const t=v.duration*(i+0.5)/n;
    await new Promise(r=>{v.onseeked=r;v.currentTime=t});
    cv.getContext("2d").drawImage(v,0,0,cv.width,cv.height);
    out.push({t,b64:cv.toDataURL("image/jpeg",0.8).split(",")[1]});
   }
   res(out);
  };
 });
}
async function watch(src,name,say){
 const vm=visionModel();if(!vm)throw new Error(NOVISION);
 say("Opening the video\u2026");
 const fr=await frames(src,6),notes=[];
 for(let i=0;i<fr.length;i++){
  say("Looking at "+name+": frame "+(i+1)+" of "+fr.length+"\u2026 (this is slow on a weak computer)");
  const t=await ask([{role:"user",content:"Describe what you see in this video frame in one or two sentences. Mention any on-screen text, people, the game or app, and what is happening.",images:[fr[i].b64]}],{num_predict:90,num_ctx:4096},vm);
  notes.push("["+fmt(fr[i].t)+"] "+t);
 }
 say("");
 return 'Notes from the video "'+name+'", described frame by frame in order:\n'+notes.join("\n");
}
async function attach(files){
 for(const f of files){
  try{
   if(f.type.startsWith("image/")){PA.imgs.push(await shrink(f));renderPA()}
   else if(f.type.startsWith("video/")){const n=await watch(URL.createObjectURL(f),f.name,t=>status(t,true));PA.notes.push(n);PA.labels.push(f.name);renderPA();status("")}
  }catch(e){status(e.message)}
 }
}

let shT=null;
function drawQR(rows){
 const cv=$("shqr"),n=rows.length,q=4,px=Math.max(5,Math.floor(260/(n+2*q))),size=(n+2*q)*px,x=cv.getContext("2d");
 cv.width=cv.height=size;x.fillStyle="#fff";x.fillRect(0,0,size,size);x.fillStyle="#000";
 rows.forEach((r,y)=>[...r].forEach((c,i)=>{if(c==="1")x.fillRect((i+q)*px,(y+q)*px,px,px)}));
}
function shRender(j){
 $("shoff").hidden=j.running;$("shon").hidden=!j.running;
 if(!j.running)return;
 drawQR(j.qr);$("shurl").textContent=j.url;
 $("shgot").innerHTML=j.got.length?j.got.map(f=>"<div>"+esc(f.name)+" ("+f.mb+" MB)</div>").join(""):"Nothing received yet.";
 const o=$("shout");o.innerHTML="";
 if(!j.out.length)o.textContent="No files added yet.";
 j.out.forEach(f=>{
  const d=document.createElement("div"),b=document.createElement("button");
  d.textContent=f.name+" ("+f.mb+" MB) ";b.textContent="\u00d7";b.setAttribute("aria-label","Remove "+f.name);b.style.cssText="background:none;border:0;color:var(--mute)";
  b.onclick=async()=>{shRender(await api("/api/share",{action:"remove",id:f.id}))};d.append(b);o.append(d);
 });
}
async function shPoll(){
 if(!$("shdlg").open){shT=null;return}
 try{shRender(await api("/api/share",{action:"status"}))}catch(e){}
 shT=setTimeout(shPoll,2000);
}
async function openShare(){
 $("shdlg").showModal();$("shmsg").textContent="";
 try{const j=await api("/api/share",{action:"status"});$("shrecv").value=j.recv;shRender(j)}catch(e){$("shmsg").textContent=e.message}
 clearTimeout(shT);shPoll();
}

const ICON={
 speak:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
 copy:'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
 check:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
 face:'<svg class="face" viewBox="180 180 620 620" aria-hidden="true"><path d="M490 232C400 232 312 320 312 450C312 570 340 700 490 700C640 700 668 570 668 450C668 320 580 232 490 232Z"/><path d="M312 448C262 462 250 568 322 592M668 448C718 462 730 568 658 592"/><path d="M410 690L388 750M573 690L592 750"/><path d="M380 432Q415 490 450 432M532 432Q567 490 602 432"/><path d="M452 578Q490 598 528 578"/><path d="M240 268L283 300M743 268L700 300M212 396H250M732 396H768"/></svg>'
};
ICON.expand='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>';
ICON.folder='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
ICON.folderplus='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M12 11v6M9 14h6"/></svg>';
ICON.dots='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></svg>';
ICON.grip='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>';
ICON.pencil='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
function abtn(svg,label,fn){
 const b=document.createElement("button");
 b.className="abtn";b.innerHTML=svg;b.title=label;b.setAttribute("aria-label",label);b.onclick=fn;return b;
}
function actions(m,d){
 const bar=document.createElement("div");bar.className="acts";
 const rd=abtn(ICON.speak,"Read aloud",()=>{
  if(rd.classList.contains("on")){speechSynthesis.cancel();return}
  rd.classList.add("on");rd.title="Stop reading";
  speak(m.content,()=>{rd.classList.remove("on");rd.title="Read aloud"});
 });
 const cp=abtn(ICON.copy,"Copy the whole reply",async()=>{
  try{await navigator.clipboard.writeText(m.content)}
  catch(e){const t=document.createElement("textarea");t.value=m.content;document.body.append(t);t.select();document.execCommand("copy");t.remove()}
  cp.innerHTML=ICON.check;cp.title="Copied";cp.setAttribute("aria-label","Copied");
  setTimeout(()=>{cp.innerHTML=ICON.copy;cp.title="Copy the whole reply";cp.setAttribute("aria-label","Copy the whole reply")},1500);
 });
 bar.append(rd,cp);
 if(!m.orig){
  if(m.content.length>350&&!m.content.includes("```"))bar.append(abtn(ICON.face,"Make it sound like me",()=>humanize(m,d)));
 }else{
  const t=document.createElement("button");
  t.className="btn ghost";t.style.cssText="padding:3px 10px;font:12px system-ui;margin-left:6px";
  t.textContent=m.on?"Show original":"Show my-voice version";
  t.onclick=()=>{[m.content,m.orig]=[m.orig,m.content];m.on=!m.on;save();render()};
  bar.append(t);
 }
 return bar;
}
function vlabels(){$("vratev").textContent=(+$("vrate").value).toFixed(1)+"x";$("vpitchv").textContent=(+$("vpitch").value).toFixed(1)}
function fillVoices(){
 if(!window.speechSynthesis)return;
 const sel=$("voicesel"),lang=(navigator.language||"en").slice(0,2);
 sel.innerHTML='<option value="">Default voice</option>';
 [...speechSynthesis.getVoices()].sort((a,b)=>(b.lang.startsWith(lang)-a.lang.startsWith(lang))||a.name.localeCompare(b.name)).forEach(v=>{
  const o=document.createElement("option");o.value=v.name;o.textContent=v.name+" ("+v.lang+")";sel.append(o);
 });
 sel.value=S.voice||"";
}

function ollText(j){
 if(!j.latest&&!j.current)return "Couldn't check. Is Ollama running, and are you online?";
 const cur=j.current?"Installed "+j.current:"Ollama isn't running",lat=j.latest?" \u00b7 Latest "+j.latest:" \u00b7 couldn't check the latest version";
 return cur+lat+(j.newer?" \u00b7 an update is available":(j.current&&j.latest?" \u00b7 up to date":""));
}
async function ollPoll(){
 try{
  const u=(await api("/api/ollama",{action:"status"})).update;
  const t=u.state==="downloading"?"Updating Ollama: downloading "+u.pct+"%":u.state==="installing"?"Updating Ollama: installing\u2026 chat may pause for a moment":(u.state==="done"||u.state==="error")?u.msg:"";
  if(t)$("ollinfo").textContent=t;
  if(u.state==="downloading"||u.state==="installing"){status(t,true);setTimeout(ollPoll,2500)}
  else if(u.state==="done"){status(t,true);setTimeout(()=>ollCheck(false),1500)}
  else if(u.state==="error")status(u.msg);
 }catch(e){}
}
async function ollCheck(auto){
 try{
  $("ollinfo").textContent="Checking\u2026";
  const j=await api("/api/ollama",{action:"check"});$("ollinfo").textContent=ollText(j);
  if(j.update&&(j.update.state==="downloading"||j.update.state==="installing")){ollPoll();return j}
  if(j.newer&&auto){
   if(j.os==="windows"){await api("/api/ollama",{action:"update"});ollPoll()}
   else status("Ollama "+j.latest+" is available. Open Settings and click Download latest Ollama.",true);
  }
  return j;
 }catch(e){$("ollinfo").textContent=e.message}
}
// ---------- code helper ----------
const CODESYS="You are an expert software engineer working on a real project on the user's computer. Be brief. When you create or change files, write EACH file in exactly this format, with nothing else inside the block:\nFILE: relative/path/to/file.ext\n```\n<the COMPLETE new contents of the file>\n```\nAlways give the whole file, never fragments or '...'. Use paths relative to the project folder. Only touch files you need to. Before the files, write one or two short sentences about what you did. If the request is only a question, answer in plain prose with no FILE blocks.";
let CODEF=[],CODEIMGS=[],CODECHG=[],busyCode=false;
function codesay(t){$("codemsg").textContent=fixErr(t)||""}
async function codeOpen(final){
 try{
  codesay("Loading\u2026");
  const j=await api("/api/code",{action:"list",folder:$("codefolder").value});CODEF=j.files;
  const box=$("codefiles");box.innerHTML="";
  CODEF.forEach(f=>{const lab=document.createElement("label"),cb=document.createElement("input"),t=document.createElement("span");lab.className="crow";cb.type="checkbox";cb.dataset.path=f.path;t.textContent=f.path+" ("+(f.kb?f.kb+" KB":"under 1 KB")+")";lab.append(cb,t);box.append(lab)});
  codesay(final||(CODEF.length?CODEF.length+" files"+(j.more?" (showing the first 400)":"")+". Tick the ones the AI should read.":"No text files found in that folder."));
 }catch(e){codesay(e.message)}
}
function diffLines(a,b){
 const A=a.split("\n"),B=b.split("\n");
 if(A.length*B.length>4e6)return A.map(l=>["-",l]).concat(B.map(l=>["+",l]));
 const dp=Array.from({length:A.length+1},()=>new Uint16Array(B.length+1));
 for(let i=A.length-1;i>=0;i--)for(let j=B.length-1;j>=0;j--)dp[i][j]=A[i]===B[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
 const out=[];let i=0,j=0;
 while(i<A.length&&j<B.length){if(A[i]===B[j]){out.push([" ",A[i]]);i++;j++}else if(dp[i+1][j]>=dp[i][j+1])out.push(["-",A[i++]]);else out.push(["+",B[j++]])}
 while(i<A.length)out.push(["-",A[i++]]);while(j<B.length)out.push(["+",B[j++]]);
 return out;
}
function diffHtml(oldT,newT,isNew){
 const rows=isNew?newT.split("\n").map(l=>["+",l]):diffLines(oldT,newT),keep=new Array(rows.length).fill(false);let add=0,del=0,html="",skip=0;
 rows.forEach((r,i)=>{if(r[0]!==" "){for(let k=Math.max(0,i-2);k<=Math.min(rows.length-1,i+2);k++)keep[k]=true;if(r[0]==="+")add++;else del++}});
 const gap=()=>{if(skip){html+='<div class="dctx">\u2026 '+skip+" unchanged line"+(skip>1?"s":"")+" \u2026</div>";skip=0}};
 rows.forEach((r,i)=>{if(!keep[i]){skip++;return}gap();html+='<div class="'+(r[0]==="+"?"dadd":r[0]==="-"?"ddel":"dctx")+'">'+(r[0]===" "?"  ":r[0]+" ")+esc(r[1])+"</div>"});
 gap();return {html,add,del};
}
function codeShow(out,old,cut,paths){
 const re=/(?:^|\n)[ \t*]*FILE:\s*([^\n]+)\n```[^\n]*\n([\s\S]*?)\n```/g;let m;CODECHG=[];
 while((m=re.exec(out)))CODECHG.push({path:m[1].trim().replace(/^[`'"*]+|[`'"*]+$/g,"").replace(/\\/g,"/"),content:m[2]+"\n"});
 let note=out.replace(re,"").trim();
 if(!CODECHG.length&&paths.length===1){const f=out.match(/```[^\n]*\n([\s\S]*?)\n```/);if(f){CODECHG.push({path:paths[0],content:f[1]+"\n"});note=out.replace(f[0],"").trim()}}
 const box=$("codeout");box.innerHTML="";
 if(note){const d=document.createElement("div");d.className="cnote";d.textContent=note;box.append(d)}
 let any=false;
 CODECHG.forEach((c,i)=>{
  c.existing=c.path in old;
  let why="";
  if(cut.has(c.path))why="The AI only saw part of this file, so rewriting it could lose the rest.";
  else if(!c.existing&&CODEF.some(f=>f.path===c.path))why="This file already exists but wasn't ticked, so the AI never saw it.";
  const d=document.createElement("div"),lab=document.createElement("label"),cb=document.createElement("input"),b=document.createElement("b"),bd=document.createElement("span");
  d.className="cchg";lab.className="crow";cb.type="checkbox";cb.dataset.i=i;cb.checked=!why;cb.disabled=!!why;
  b.textContent=c.path;bd.className="cbadge";bd.textContent=why?"blocked":c.existing?"changed":"new file";
  lab.append(cb,b,bd);d.append(lab);
  if(why){const w=document.createElement("div");w.className="cwhy";w.textContent=why;d.append(w)}
  else{
   if(/(\.\.\.|\u2026)\s*(rest|existing|unchanged|same|previous)|^\s*(\/\/|#)\s*\.\.\./im.test(c.content)){const w=document.createElement("div");w.className="cwhy";w.textContent="This may be incomplete (it contains '...'). Check the changes before applying.";d.append(w);bd.classList.add("warn")}
   const df=diffHtml(old[c.path]||"",c.content,!c.existing),det=document.createElement("details"),sm=document.createElement("summary"),pre=document.createElement("pre");
   sm.textContent="Show changes (+"+df.add+" \u2212"+df.del+")";pre.className="diff";pre.innerHTML=df.html;det.append(sm,pre);d.append(det);any=true;
  }
  box.append(d);
 });
 $("codeapply").hidden=!any;
 if(!CODECHG.length&&!note)codesay("The AI didn't return anything.");
}
async function codeAsk(){
 if(busyCode){if(ctl)ctl.abort();return}
 const req=$("codereq").value.trim();if(!req){codesay("Say what you want done first.");return}
 const paths=[...$("codefiles").querySelectorAll("input:checked")].map(x=>x.dataset.path);
 try{
  busyCode=true;$("codego").textContent="Stop";$("codeout").innerHTML="";$("codeapply").hidden=true;codesay("Reading files\u2026");
  const files=paths.length?(await api("/api/code",{action:"read",folder:$("codefolder").value,paths})).files:{};
  let budget=36000,ctx="";const cut=new Set(),old={};
  for(const p of paths){
   const f=files[p];if(!f)continue;
   let t=f.text;old[p]=f.text;
   if(f.cut||t.length>budget){t=t.slice(0,Math.max(0,budget));cut.add(p)}
   budget-=t.length;
   ctx+="=== "+p+(cut.has(p)?" (shortened, so do not rewrite this file)":"")+" ===\n```\n"+t+"\n```\n\n";
  }
  const user="Project files:\n"+CODEF.slice(0,200).map(f=>f.path).join("\n")+"\n\n"+(ctx?"Files you can read:\n"+ctx:"(No files were ticked, so you cannot see any file contents.)\n\n")+"Request: "+req;
  const img=CODEIMGS.length>0,model=img?visionModel():mkModel();if(!model)throw new Error(NOVISION);
  codesay("Writing with "+model+"\u2026");
  const out=await gen([{role:"system",content:CODESYS},{role:"user",content:user,images:img?CODEIMGS:undefined}],t=>codesay("Writing with "+model+"\u2026 "+t.length+" characters"),{num_ctx:16384,temperature:0.2,model});
  codesay("Review the changes below. Nothing has been written yet.");codeShow(out,old,cut,paths);
 }catch(e){codesay(e.name==="AbortError"?"Stopped.":e.message)}
 busyCode=false;$("codego").textContent="Ask";
}
function renderCodeImgs(){
 const b=$("codeimgs");b.innerHTML="";
 CODEIMGS.forEach((g,i)=>{const im=document.createElement("img");im.src="data:image/jpeg;base64,"+g;im.title="Click to remove";im.style.cssText="height:44px;border-radius:6px;cursor:pointer";im.onclick=()=>{CODEIMGS.splice(i,1);renderCodeImgs()};b.append(im)});
}

// ---------- computer helper ----------
let AUTOPLAN=null,autoT=null;
const autoSys=(w,h,shot)=>"You turn a request into mouse and keyboard steps for a computer. "+(shot?"You are given a screenshot of the screen ("+w+" by "+h+" pixels). Every x and y you write must be a pixel position in that screenshot.":"The screen is "+w+" by "+h+" pixels, but you cannot see it.")+" Reply with ONLY one JSON object and no other text: {\"summary\":\"short description\",\"repeat\":1,\"steps\":[...]}. Allowed steps: {\"op\":\"move\",\"x\":0,\"y\":0} {\"op\":\"click\",\"x\":0,\"y\":0} (leave out x and y to click where the mouse already is) {\"op\":\"double_click\"} {\"op\":\"right_click\"} {\"op\":\"drag\",\"x\":0,\"y\":0,\"x2\":0,\"y2\":0} {\"op\":\"scroll\",\"amount\":-300} (positive scrolls up, negative scrolls down, about 100 per notch) {\"op\":\"type\",\"text\":\"hello\"} {\"op\":\"press\",\"key\":\"enter\"} {\"op\":\"hotkey\",\"keys\":[\"ctrl\",\"s\"]} {\"op\":\"wait\",\"seconds\":2}. Prefer keyboard shortcuts over clicking. Never invent a position for something you cannot see: if the task needs a position you were not given, reply {\"error\":\"Tell me the x and y position of ...\"} instead. Use \"repeat\" (up to 100) to repeat the whole list. Keep it under 40 steps.";
async function autoInfo(){try{const j=await api("/api/auto",{action:"info"});$("autoinfo").textContent="Ready. Your screen is "+j.w+" by "+j.h+" pixels."}catch(e){$("autoinfo").textContent=e.message}}
function openAuto(){$("autodlg").showModal();autoInfo()}
async function autoShow(plan){
 const j=await api("/api/auto",{action:"validate",plan});AUTOPLAN=j.plan;
 $("autosum").textContent=(j.plan.summary||"Steps")+(j.plan.repeat>1?" (all repeated "+j.plan.repeat+" times)":"");
 $("autolines").innerHTML=j.lines.map(l=>"<li>"+esc(l)+"</li>").join("");
 $("autojson").value=JSON.stringify(j.plan,null,1);$("autoplan").hidden=false;
}
async function autoPoll(){
 try{
  const j=await api("/api/auto",{action:"status"});$("autostat").textContent=j.msg||"";
  if(j.state==="countdown"||j.state==="running")autoT=setTimeout(autoPoll,500);
  else{$("autostop").hidden=true;$("autorun").disabled=false}
 }catch(e){}
}

// ---------- test that images and videos can be read ----------
function testVideo(){
 return new Promise((res,rej)=>{
  const cv=document.createElement("canvas");cv.width=320;cv.height=180;const x=cv.getContext("2d");
  if(!window.MediaRecorder||!cv.captureStream)return rej(new Error("This browser can't make the test video."));
  const rec=new MediaRecorder(cv.captureStream(15)),chunks=[];
  rec.ondataavailable=e=>chunks.push(e.data);rec.onstop=()=>res(new Blob(chunks,{type:"video/webm"}));
  const t0=performance.now();
  const draw=()=>{const t=performance.now()-t0,go=t<1000;x.fillStyle=go?"#fff":"#222";x.fillRect(0,0,320,180);x.fillStyle=go?"#000":"#fff";x.font="bold 80px sans-serif";x.fillText(go?"GO":"STOP",go?100:40,115);if(t<2000)requestAnimationFrame(draw);else rec.stop()};
  rec.start();draw();
 });
}
async function visionTest(){
 const out=$("vtres");out.innerHTML="";$("vtbtn").disabled=true;
 const add=(k,t)=>{const d=document.createElement("div");d.textContent=({ok:"\u2713 ",bad:"\u2717 ",info:"\u2022 "}[k])+t;out.append(d)};
 try{
  const vm=visionModel();
  if(!vm){add("bad",NOVISION);return}
  add("info","Models that can see: "+VISION.join(", ")+". Testing with "+vm+". This can take a minute or two.");
  try{
   const cv=document.createElement("canvas");cv.width=480;cv.height=270;const x=cv.getContext("2d");
   x.fillStyle="#fff";x.fillRect(0,0,480,270);x.fillStyle="#000";x.font="bold 90px sans-serif";x.fillText("ATLAS 42",40,130);x.fillStyle="#d22";x.beginPath();x.arc(240,205,42,0,7);x.fill();
   const a=await ask([{role:"user",content:"What text is written in this picture, and what color is the circle? Answer in one short sentence.",images:[cv.toDataURL("image/jpeg",.9).split(",")[1]]}],{num_predict:60,num_ctx:4096},vm);
   add(/42|atlas/i.test(a)?"ok":"bad",(/42|atlas/i.test(a)?"Images work. ":"It answered but didn't read the text right. ")+"It said: "+a.trim());
  }catch(e){add("bad","Images: "+fixErr(e.message));return}
  try{
   add("info","Making a 2 second test video\u2026");
   const fr=await frames(URL.createObjectURL(await testVideo()),2),got=[];
   for(const f of fr)got.push(await ask([{role:"user",content:"What single word is written in this picture? Reply with only that word.",images:[f.b64]}],{num_predict:10,num_ctx:4096},vm));
   const ok=got.length===2&&/go/i.test(got[0])&&/stop/i.test(got[1]);
   add(ok?"ok":"bad",(ok?"Videos work. ":"Frames were taken, but they weren't read right (expected GO then STOP). ")+"It saw: "+got.map(g=>'"'+g.trim()+'"').join(" then "));
  }catch(e){add("bad","Videos: "+fixErr(e.message))}
 }finally{$("vtbtn").disabled=false}
}

// ======================= Cyber Trainer =======================
function ipInt(s){const m=(s||"").trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);if(!m)return null;const o=m.slice(1).map(Number);if(o.some(x=>x>255))return null;return ((o[0]<<24)|(o[1]<<16)|(o[2]<<8)|o[3])>>>0}
function intIp(n){return [(n>>>24)&255,(n>>>16)&255,(n>>>8)&255,n&255].join(".")}
function cidrMask(c){return c===0?0:(0xFFFFFFFF<<(32-c))>>>0}
function maskCidr(s){const n=ipInt(s);if(n===null)return null;let c=0,seenZero=false;for(let i=31;i>=0;i--){const b=(n>>>i)&1;if(b){if(seenZero)return null;c++}else seenZero=true}return c}
function netCalc(ip,cidr){
 const mask=cidrMask(cidr),net=(ip&mask)>>>0,bc=(net|(~mask>>>0))>>>0;
 const hosts=cidr>=31?(cidr===32?1:2):Math.pow(2,32-cidr)-2;
 const first=cidr>=31?net:(net+1)>>>0,last=cidr>=31?bc:(bc-1)>>>0;
 return {mask,net,bc,hosts,first,last};
}
const CYTRACKS=[
 {id:"basics",title:"Cybersecurity Basics",emoji:"\u{1f6e1}\ufe0f",steps:[
  {t:"info",title:"The CIA Triad",body:"Almost everything in security protects one of three things: Confidentiality (only the right people can read it), Integrity (the data hasn't been tampered with), and Availability (it's there when you need it). When you hear about an attack, ask which of these three it broke."},
  {t:"quiz",q:"A website gets knocked offline by a flood of fake traffic (a DoS attack). Which part of the CIA triad does this attack on?",choices:["Confidentiality","Integrity","Availability"],a:2,why:"The data wasn't read or changed \u2014 it just wasn't reachable. That's an availability attack."},
  {t:"quiz",q:"An attacker secretly changes the amount in a bank transfer before it's processed. Which part of the triad is violated?",choices:["Confidentiality","Integrity","Availability"],a:1,why:"The data was altered without authorization \u2014 that's an integrity violation, even though nothing was 'stolen'."},
  {t:"info",title:"Threat, Vulnerability, Risk",body:"These three words get mixed up constantly, including on exams. A vulnerability is a weakness (an unpatched server). A threat is something that could exploit it (an attacker, or even a flood). Risk is the likelihood times the impact if that threat hits that vulnerability. No vulnerability, no risk \u2014 even with a threat around."},
  {t:"quiz",q:"An unpatched piece of software with a known flaw is best described as a:",choices:["Threat","Vulnerability","Risk","Exploit"],a:1,why:"The weakness itself is the vulnerability. The thing that could take advantage of it is the threat."},
  {t:"info",title:"Authentication factors",body:"Authentication proves who you are. The three classic factors: something you know (password), something you have (phone, token), something you are (fingerprint). Multi-factor authentication (MFA) combines at least two different factors \u2014 two passwords is NOT MFA, because they're the same factor type."},
  {t:"quiz",q:"Logging in with a password plus a code from an authenticator app uses which two factors?",choices:["Something you know + something you know","Something you know + something you have","Something you have + something you are","All three factors"],a:1,why:"Password = know. Code from an app on your phone = have. That's true MFA."},
  {t:"info",title:"Common malware types",body:"Virus: attaches to a host file and needs a human to run it. Worm: spreads on its own across a network, no human needed. Trojan: disguises itself as something legitimate. Ransomware: encrypts your files and demands payment. Spyware: quietly collects information."},
  {t:"quiz",q:"Malware that spreads across a network by itself, without a user opening anything, is best classified as a:",choices:["Virus","Worm","Trojan","Adware"],a:1,why:"Self-propagation with no human action is the defining trait of a worm."},
  {t:"info",title:"Symmetric vs asymmetric encryption",body:"Symmetric encryption uses ONE shared key to both lock and unlock data \u2014 fast, but you have to get the key to the other person safely (like AES). Asymmetric uses a key PAIR: a public key anyone can use to encrypt, and a private key only you hold to decrypt (like RSA). HTTPS actually uses both: asymmetric to exchange a key, then symmetric for the actual data."},
  {t:"quiz",q:"Which type of encryption uses a public key and a private key?",choices:["Symmetric","Asymmetric","Hashing","Steganography"],a:1,why:"A key pair \u2014 public to encrypt, private to decrypt \u2014 is the definition of asymmetric encryption."}
 ]},
 {id:"network",title:"Networking & Subnetting",emoji:"\ud83d\udd0c",steps:[
  {t:"info",title:"IP addresses and octets",body:"An IPv4 address like 192.168.1.10 is 32 bits, written as four 8-bit \"octets\" (0\u2013255) separated by dots. In binary, each octet is 8 ones and zeros: 192 = 11000000, 1 = 00000001. Subnetting is really just binary math wearing a decimal costume."},
  {t:"quiz",q:"What is 192 in binary?",choices:["11000000","11100000","10100000","11001000"],a:0,why:"128+64 = 192, so the first two bits are 1 and the rest are 0: 11000000."},
  {t:"info",title:"What a subnet mask actually does",body:"A subnet mask is a run of 1s followed by a run of 0s, like 255.255.255.0 (24 ones). The 1s mark the NETWORK part of the address; the 0s mark the HOST part. CIDR notation (/24) is just a shorthand for \"24 ones in the mask\"."},
  {t:"quiz",q:"A /26 subnet mask has how many bits set to 1?",choices:["24","26","6","32"],a:1,why:"The number after the slash IS the count of 1-bits in the mask. /26 means 26 ones, 6 zeros."},
  {t:"info",title:"Finding the network address",body:"To find the network address, line the IP up with the mask in binary and AND them together (1 AND 1 = 1, anything AND 0 = 0). In practice: for a mask that lands on a whole octet (/8, /16, /24), just zero out the host octets. For masks in between, you need the 'magic number' trick below."},
  {t:"info",title:"The magic number trick",body:"For a mask inside one octet, subtract the mask's last non-255 value from 256 \u2014 that's your 'block size'. Example: /27 mask is 255.255.255.224. 256-224=32. Subnets jump by 32 in that octet: 0, 32, 64, 96, 128, 160, 192, 224. Find which block your address falls into \u2014 that's your network address."},
  {t:"quiz",q:"192.168.1.77 /27 \u2014 the block size is 32 (0,32,64,96...). Which block does .77 fall into, and what's the network address?",choices:["192.168.1.64","192.168.1.32","192.168.1.96","192.168.1.0"],a:0,why:"77 is between 64 and 96, so it falls in the 64 block. Network address = 192.168.1.64."},
  {t:"quiz",q:"Same subnet, 192.168.1.64 /27. What is the broadcast address?",choices:["192.168.1.95","192.168.1.127","192.168.1.63","192.168.1.255"],a:0,why:"The broadcast is one less than the START of the NEXT block. Next block starts at 96, so broadcast = 96-1 = 95."},
  {t:"info",title:"Usable hosts",body:"Every subnet loses exactly 2 addresses: the network address (identifies the subnet itself) and the broadcast address (sends to everyone on it). Formula: 2^(host bits) - 2. A /27 has 5 host bits (32-27), so 2^5-2 = 30 usable hosts."},
  {t:"quiz",q:"How many usable host addresses are in a /29 network?",choices:["8","6","4","2"],a:1,why:"/29 leaves 3 host bits (32-29=3). 2^3=8, minus the network and broadcast = 6 usable."},
  {t:"info",title:"Picking a mask from a host requirement",body:"If you're told 'this department needs 20 hosts', find the smallest power of 2, minus 2, that's at least 20. 2^5-2=30 \u2265 20 works; 2^4-2=14 is too small. 5 host bits means a /27 (32-5)."},
  {t:"quiz",q:"A subnet needs to support 50 hosts. What's the smallest subnet (as a CIDR) that fits?",choices:["/25","/26","/27","/24"],a:1,why:"/26 gives 6 host bits: 2^6-2=62 \u2265 50. /27 only gives 30, too small."}
 ]},
 {id:"tools",title:"Tools of the Trade",emoji:"\ud83e\uddf0",steps:[
  {t:"info",title:"Before anything else: get authorization",body:"Every tool below is legal to learn and legal to use \u2014 ON SYSTEMS YOU OWN OR HAVE WRITTEN PERMISSION TO TEST. Running these against someone else's network without permission is a crime in most countries (in the US, the Computer Fraud and Abuse Act), even if you don't cause any damage. In the workforce this permission is called a 'scope' or 'rules of engagement' document, and you stay inside it."},
  {t:"quiz",q:"You're curious about a classmate's Wi-Fi security. What should you do first?",choices:["Scan it quietly, you're just learning","Ask for their written permission first, or use your own lab instead","It's fine since you won't change anything","Only illegal if you get caught"],a:1,why:"Scanning someone else's network without permission is unauthorized access, even just to look. Use your own gear or a permitted lab (like a CTF or Hack The Box) instead."},
  {t:"info",title:"Nmap \u2014 the network mapper",body:"Nmap finds what's alive on a network and what's running on it. `nmap -sV 192.168.1.0/24` scans a whole subnet and tries to identify service versions on any open ports it finds. It's usually the very first tool run in a penetration test, to build a map of the target before anything else happens."},
  {t:"quiz",q:"What does Nmap's -sV flag do?",choices:["Scans very fast, skipping checks","Tries to detect the version of each service it finds open","Saves the output to a file","Scans only well-known ports"],a:1,why:"-sV is version detection \u2014 it fingerprints what's actually running on each open port, like 'Apache 2.4.41', not just that the port is open."},
  {t:"info",title:"Wireshark \u2014 seeing the traffic itself",body:"Wireshark captures and displays every packet crossing a network interface. You use filters to cut through the noise, like `http.request` to show only web requests, or `ip.addr==10.0.0.5` to focus on one machine. It's how you prove what actually happened on a wire, not what should have happened."},
  {t:"quiz",q:"You want to see only DNS traffic in a Wireshark capture. Which filter does that?",choices:["tcp.port==80","dns","icmp","frame contains \"query\""],a:1,why:"Typing the protocol name, `dns`, filters the view to just DNS packets \u2014 the simplest and most common Wireshark filter."},
  {t:"info",title:"Burp Suite / OWASP ZAP \u2014 testing web apps",body:"These tools sit between your browser and a website as a 'proxy', letting you see and edit every request before it's sent. This is how testers find things like a shopping cart that trusts a price sent from the browser instead of checking it on the server \u2014 you just edit the request and see what the server accepts."},
  {t:"quiz",q:"What role does Burp Suite play when testing a web app?",choices:["It automatically breaks into the server","An intercepting proxy, so you can see and edit requests before they're sent","A firewall that blocks attacks","A password cracker"],a:1,why:"Burp sits in the middle (a proxy) so you can inspect and modify what your browser is actually sending, which reveals how the server really behaves."},
  {t:"info",title:"Metasploit \u2014 the exploit framework",body:"Metasploit is a huge library of known, already-public vulnerabilities packaged into ready-to-run 'modules', used to confirm a flaw is really exploitable rather than just theoretical (which is what a vulnerability scanner alone tells you). In the real world, this only ever runs inside an authorized engagement, against systems named in the scope."},
  {t:"quiz",q:"A vulnerability scanner says a server 'might' be vulnerable to a known flaw. What does running the matching Metasploit module (with authorization) tell you that the scanner alone can't?",choices:["Nothing new, it's the same result","Whether the flaw can actually be exploited in practice, not just theoretically present","The exact password of the admin account","That the server should be deleted"],a:1,why:"Scanners often report based on version numbers alone (false positives are common). Actually running the exploit (with permission) proves whether it truly works."},
  {t:"info",title:"John the Ripper / Hashcat \u2014 password auditing",body:"Organizations use these tools on their OWN password hashes to find weak passwords before an attacker does \u2014 for example, auditing that 'Password123' is still in use somewhere. They try huge lists of common passwords (dictionaries) and pattern variations, never 'the' password hash of an account you don't control."},
  {t:"quiz",q:"A company runs Hashcat against its own employee password hashes. What is the legitimate purpose?",choices:["To steal employee passwords for fun","To find weak passwords internally before a real attacker does","It has no legitimate use","To decrypt customer credit cards"],a:1,why:"This is proactive password auditing \u2014 finding your own weak passwords first is standard, authorized security hygiene."},
  {t:"info",title:"tcpdump & netcat \u2014 the Swiss army knives",body:"tcpdump is Wireshark's command-line cousin, for capturing packets over SSH on a server with no GUI. Netcat (`nc`) reads and writes raw data over a network connection \u2014 testers use it to check if a port is really open and talking (`nc -zv host port`) or to move files between two machines during an authorized test."},
  {t:"quiz",q:"What does `nc -zv 10.0.0.5 22` do?",choices:["Deletes port 22","Checks (verbosely) whether port 22 on that host is open, without sending data","Opens a reverse shell automatically","Scans every port on the host"],a:1,why:"-z means 'zero-I/O', just check if the port responds, and -v is verbose output. It's the simplest single-port check there is."}
 ]}
];
function cyProg(){S.cyber=S.cyber||{done:{},subnet:{right:0,total:0}};return S.cyber}
function cyTrackPct(tr){const d=cyProg().done[tr.id]||0;return Math.round(100*d/tr.steps.length)}
function cyRenderHome(){
 const p=cyProg(),box=$("cytracks");box.innerHTML="";
 CYTRACKS.forEach(tr=>{
  const pct=cyTrackPct(tr),done=p.done[tr.id]||0;
  const c=document.createElement("button");c.className="cytrack";
  c.innerHTML='<div class="ct-top"><span>'+tr.emoji+'</span><span style="font-size:13px;color:var(--mute)">'+pct+'%</span></div><div class="ct-title">'+esc(tr.title)+'</div><div class="ct-bar"><i style="width:'+pct+'%"></i></div><div class="ct-n">'+done+' of '+tr.steps.length+' done'+(done>=tr.steps.length?" \u2713":"")+'</div>';
  c.onclick=()=>cyStart(tr);box.append(c);
 });
 const st=p.subnet;$("cystats").textContent=st.total?("So far: "+st.right+" of "+st.total+" correct ("+Math.round(100*st.right/st.total)+"%)."):"No drills yet \u2014 start when ready.";
}
let CYTR=null,CYI=0,CYRIGHT=0,CYDRILL=false,CYQ=null;
function cyOpen(){$("cyberdlg").showModal();cyRenderHome()}
function cyStart(tr){CYTR=tr;CYI=cyProg().done[tr.id]||0;if(CYI>=tr.steps.length)CYI=0;CYRIGHT=0;CYDRILL=false;$("cyhome").hidden=true;$("cyplay").hidden=false;cyRenderStep()}
function cyExit(){$("cyplay").hidden=true;$("cyhome").hidden=false;cyRenderHome()}
function cyBump(n,d){CYI=n;if(d>(cyProg().done[CYTR.id]||0))cyProg().done[CYTR.id]=d;save()}
function cyAskRow(context){
 const box=document.createElement("div");box.className="cyaskbox";
 const b=document.createElement("button");b.className="cyask";b.textContent="\ud83e\udd16 Ask Atlas to explain";
 const area=document.createElement("div");area.hidden=true;
 const ta=document.createElement("textarea");ta.rows=2;ta.placeholder="Optional: say exactly what's confusing you";
 const go=document.createElement("button");go.className="btn ghost";go.style.marginTop="6px";go.textContent="Ask";
 const out=document.createElement("div");out.className="cytxt";
 go.onclick=async()=>{
  go.disabled=true;out.textContent="Thinking\u2026";
  try{
   const t=await ask([{role:"system",content:"You are a patient, encouraging cybersecurity tutor helping a college student cram for finals. Keep answers short, concrete, and example-driven. Never just repeat the question back."},
    {role:"user",content:context+(ta.value.trim()?("\n\nThe student specifically says: "+ta.value.trim()):"\n\nExplain this simply, as if to someone who just got it wrong once.")}],{temperature:0.4,num_ctx:4096});
   out.textContent=t.trim();
  }catch(e){out.textContent=fixErr(e.message)}
  go.disabled=false;
 };
 b.onclick=()=>{area.hidden=!area.hidden;if(!area.hidden)ta.focus()};
 area.append(ta,go,out);box.append(b,area);return box;
}
function cyRenderStep(){
 const steps=CYTR.steps,st=steps[CYI],box=$("cycard");box.innerHTML="";
 $("cyprogfill").style.width=Math.round(100*CYI/steps.length)+"%";
 $("cyscore").textContent=CYTR.title+" \u00b7 "+(CYI+1)+" of "+steps.length;
 if(st.t==="info"){
  const d=document.createElement("div");d.className="cycard";
  d.innerHTML="<h3>"+esc(st.title)+"</h3><p>"+esc(st.body)+"</p>";
  d.append(cyAskRow("Lesson topic: "+st.title+"\n"+st.body));
  const row=document.createElement("div");row.className="cyrow";
  const next=document.createElement("button");next.className="btn";next.textContent=CYI+1<steps.length?"Continue":"Finish";
  next.onclick=()=>{if(CYI+1>=steps.length){cyBump(CYI,steps.length);cyExit()}else{cyBump(CYI+1,CYI+1);cyRenderStep()}};
  row.append(next);d.append(row);box.append(d);
 }else{
  const d=document.createElement("div");d.className="cycard cyq";
  d.innerHTML='<div class="q">'+esc(st.q)+"</div>";
  st.choices.forEach((c,i)=>{
   const b=document.createElement("button");b.className="cyopt";b.textContent=c;
   b.onclick=()=>{
    [...d.querySelectorAll(".cyopt")].forEach(x=>x.disabled=true);
    const ok=i===st.a;b.classList.add(ok?"right":"wrong");
    if(!ok)d.querySelector(".cyopt:nth-child("+(st.a+2)+")").classList.add("right");
    const f=document.createElement("div");f.className="cyfeed "+(ok?"right":"wrong");f.textContent=(ok?"Correct. ":"Not quite. ")+st.why;d.append(f);
    d.append(cyAskRow("Question: "+st.q+"\nCorrect answer: "+st.choices[st.a]+"\nExplanation already given: "+st.why+(ok?"\nThe student got it right but wants more depth.":"\nThe student answered '"+c+"' and got it wrong.")));
    const row=document.createElement("div");row.className="cyrow";
    const next=document.createElement("button");next.className="btn";next.textContent=CYI+1<steps.length?"Continue":"Finish";
    next.onclick=()=>{if(CYI+1>=steps.length){cyBump(CYI,steps.length);cyExit()}else{cyBump(CYI+1,CYI+1);cyRenderStep()}};
    row.append(next);d.append(row);
   };
   d.append(b);
  });
  box.append(d);
 }
}
function cyGen(level){
 const oct=()=>Math.floor(Math.random()*255)+1;
 const cr=level==="easy"?[24,25,26,27,28]:level==="hard"?[8,12,16,20,22,24,27,29,30]:[16,18,20,22,24,26,27,28,29,30];
 const cidr=cr[Math.floor(Math.random()*cr.length)];
 const ip=((oct()<<24)|(oct()<<16)|(oct()<<8)|oct())>>>0;
 const {net,bc,hosts,mask,first,last}=netCalc(ip,cidr);
 const kinds=level==="hard"?["net","bc","hosts","mask","reqcidr","host"]:["net","bc","hosts","mask"];
 const kind=kinds[Math.floor(Math.random()*kinds.length)];
 let q,answer,type="ip",explain;
 if(kind==="net"){q="What is the network address of "+intIp(ip)+" /"+cidr+"?";answer=intIp(net);explain="Mask is "+intIp(mask)+". AND the IP with the mask (or use the block-size trick) to get "+intIp(net)+".";}
 else if(kind==="bc"){q="What is the broadcast address for "+intIp(net)+" /"+cidr+"?";answer=intIp(bc);explain="The broadcast is the last address before the next block starts: "+intIp(bc)+".";}
 else if(kind==="hosts"){q="How many usable host addresses are in a /"+cidr+" network?";answer=String(hosts);type="num";explain="Host bits = 32-"+cidr+" = "+(32-cidr)+". 2^"+(32-cidr)+" - 2 = "+hosts+".";}
 else if(kind==="mask"){q="What is the dotted-decimal subnet mask for /"+cidr+"?";answer=intIp(mask);explain="/"+cidr+" means "+cidr+" one-bits, which is "+intIp(mask)+".";}
 else if(kind==="host"){q="Is "+intIp((net+Math.max(1,Math.min(hosts,3)))>>>0)+" a valid HOST address on the "+intIp(net)+"/"+cidr+" network? Type yes or no.";type="yn";answer=((net+Math.max(1,Math.min(hosts,3)))>>>0)!==net&&((net+Math.max(1,Math.min(hosts,3)))>>>0)!==bc?"yes":"no";explain="Valid hosts are everything strictly between the network address ("+intIp(net)+") and broadcast ("+intIp(bc)+").";}
 else{const need=[8,20,50,100,500][Math.floor(Math.random()*5)];let need_cidr=32;for(let hb=0;hb<=24;hb++){if(Math.pow(2,hb)-2>=need){need_cidr=32-hb;break}}
  q="A department needs at least "+need+" usable hosts. What's the smallest subnet, as a CIDR like /24, that fits?";type="cidr";answer="/"+need_cidr;explain="Find the smallest host-bit count hb where 2^hb-2 \u2265 "+need+". That gives /"+need_cidr+".";}
 return {q,answer,type,explain};
}
function cyDrillStart(){CYDRILL=true;$("cyhome").hidden=true;$("cyplay").hidden=false;$("cyback").onclick=()=>{CYDRILL=false;cyExit();$("cyback").onclick=cyExit};cyNextQ()}
function cyNextQ(){
 CYQ=cyGen($("cydiff").value);
 const d=document.createElement("div");d.className="cycard";
 $("cyprogfill").style.width="100%";const p=cyProg().subnet;$("cyscore").textContent="Subnetting Drill \u00b7 "+p.right+"/"+p.total+" correct";
 d.innerHTML='<div class="q" style="font-size:19px;font-weight:700;margin-bottom:14px">'+esc(CYQ.q)+"</div>";
 const inp=document.createElement("input");inp.className="cyinput";inp.placeholder=CYQ.type==="yn"?"yes or no":CYQ.type==="num"?"a number":CYQ.type==="cidr"?"e.g. /26":"e.g. 192.168.1.0";
 const go=document.createElement("button");go.className="btn";go.textContent="Check";go.style.marginTop="10px";
 const fb=document.createElement("div");
 const row=document.createElement("div");row.className="cyrow";
 const check=()=>{
  const norm=v=>String(v).trim().toLowerCase().replace(/^\/*/,"/").replace(/^\/$/,"");
  const got=CYQ.type==="cidr"?norm(inp.value):String(inp.value).trim().toLowerCase();
  const want=CYQ.type==="cidr"?norm(CYQ.answer):String(CYQ.answer).toLowerCase();
  const ok=got===want||got===want.replace("/","");
  inp.disabled=true;go.disabled=true;inp.classList.add(ok?"right":"wrong");
  const p=cyProg().subnet;p.total++;if(ok)p.right++;save();
  fb.className="cyfeed "+(ok?"right":"wrong");fb.textContent=(ok?"Correct! ":"Not quite \u2014 the answer is "+CYQ.answer+". ")+CYQ.explain;
  d.append(cyAskRow("Subnetting practice question: "+CYQ.q+"\nCorrect answer: "+CYQ.answer+"\nShort explanation given: "+CYQ.explain+(ok?"\nThe student got it right.":"\nThe student answered '"+inp.value+"' and got it wrong.")));
  const next=document.createElement("button");next.className="btn";next.textContent="Next question";next.onclick=cyNextQ;row.append(next);d.append(row);
 };
 inp.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();check()}};
 go.onclick=check;row.append(go);d.append(inp,document.createElement("br"),fb);
 const box=$("cycard");box.innerHTML="";box.append(d);setTimeout(()=>inp.focus(),0);
}

// ======================= customizable tools menu =======================
const TOOLS=[...document.querySelectorAll('#toolmenu [role="menuitem"]')].map(b=>({id:b.id,b:b.querySelector("b").textContent,span:b.querySelector("span").textContent,el:b}));
const TOOLEL={};TOOLS.forEach(t=>TOOLEL[t.id]=t.el);
function defaultMenuGroups(){return [
 {id:"g1",name:"Create",tools:["makegame","codebtn"]},
 {id:"g2",name:"Learn",tools:["cyberbtn"]},
 {id:"g3",name:"Automate",tools:["autobtn"]},
 {id:"g4",name:"Files",tools:["vids","convbtn","tidy","docsbtn"]},
 {id:"g5",name:"Share",tools:["sharebtn","gh"]}
]}
function menuGroups(){if(!S.menuGroups)S.menuGroups=defaultMenuGroups();return S.menuGroups}
const MGCHEV='<svg class="mgchev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';
const OPENSET=new Set();
function mFold(name,gid,ids,wrap,placed){
 const open=OPENSET.has(gid);
 const h=document.createElement("button");h.type="button";h.className="mgroup";h.setAttribute("aria-expanded",open);
 h.innerHTML='<span class="mgname">'+esc(name)+"</span>"+MGCHEV;
 const body=document.createElement("div");body.className="mgbody"+(open?" open":"");
 ids.forEach(id=>{const btn=TOOLEL[id];if(btn){body.append(btn);placed.add(id)}});
 h.onclick=()=>{const o=!body.classList.contains("open");body.classList.toggle("open",o);h.setAttribute("aria-expanded",o);if(o)OPENSET.add(gid);else OPENSET.delete(gid)};
 wrap.append(h,body);
}
function menuRender(){
 const groups=menuGroups(),wrap=$("toolmenu"),placed=new Set();
 wrap.innerHTML="";
 groups.forEach(g=>{if(g.tools.length)mFold(g.name,g.id,g.tools,wrap,placed)});
 const left=TOOLS.filter(t=>!placed.has(t.id));
 if(left.length)mFold("More","__more",left.map(t=>t.id),wrap,placed);
}
function mSave(){save();menuRender()}
let dragTool=null;
function mChip(t){const c=document.createElement("div");c.className="mchip";c.draggable=true;c.title=t.span;const s=document.createElement("span");s.textContent=t.b;c.append(s);c.ondragstart=()=>{dragTool=t.id};return c}
function mRenderFolders(){
 const box=$("mfolders");box.innerHTML="";const placed=new Set();
 menuGroups().forEach(g=>{
  g.tools.forEach(id=>placed.add(id));
  const d=document.createElement("div");d.className="mfold";
  const head=document.createElement("div");head.className="mfhead";
  const nm=document.createElement("span");nm.className="mfname";nm.textContent=g.name;
  const ren=document.createElement("button");ren.innerHTML=ICON.pencil;ren.title="Rename";ren.setAttribute("aria-label","Rename "+g.name);
  const del=document.createElement("button");del.innerHTML="&times;";del.title="Delete folder";del.setAttribute("aria-label","Delete "+g.name);
  ren.onclick=()=>{
   const i=document.createElement("input");i.className="mfedit";i.value=g.name;i.maxLength=40;i.setAttribute("aria-label","Folder name");
   head.replaceChild(i,nm);i.focus();i.select();
   const fin=doSave=>{if(doSave){const v=i.value.trim();if(v)g.name=v}mSave();mRenderFolders()};
   i.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();fin(true)}else if(e.key==="Escape"){e.preventDefault();fin(false)}};
   i.onblur=()=>fin(true);
  };
  del.onclick=()=>{S.menuGroups=menuGroups().filter(x=>x!==g);mSave();mRenderFolders()};
  head.append(nm,ren,del);d.append(head);
  const chips=document.createElement("div");chips.className="mchips";
  g.tools.forEach(id=>{const t=TOOLS.find(x=>x.id===id);if(t)chips.append(mChip(t))});
  d.append(chips);
  d.addEventListener("dragover",e=>{e.preventDefault();d.classList.add("drop")});
  d.addEventListener("dragleave",e=>{if(!d.contains(e.relatedTarget))d.classList.remove("drop")});
  d.addEventListener("drop",e=>{e.preventDefault();d.classList.remove("drop");mMoveTool(dragTool,g.id)});
  box.append(d);
 });
 const un=$("munsorted");un.innerHTML="";
 TOOLS.filter(t=>!placed.has(t.id)).forEach(t=>un.append(mChip(t)));
 const unwrap=un.parentElement;
 unwrap.ondragover=e=>{e.preventDefault();unwrap.classList.add("drop")};
 unwrap.ondragleave=e=>{if(!unwrap.contains(e.relatedTarget))unwrap.classList.remove("drop")};
 unwrap.ondrop=e=>{e.preventDefault();unwrap.classList.remove("drop");mMoveTool(dragTool,null)};
}
function mMoveTool(id,toGid){
 if(!id)return;
 menuGroups().forEach(g=>{g.tools=g.tools.filter(x=>x!==id)});
 if(toGid){const g=menuGroups().find(x=>x.id===toGid);if(g)g.tools.push(id)}
 mSave();mRenderFolders();
}
function openMenuEdit(){mRenderFolders();$("mmsg").textContent="";$("menudlg").showModal()}
function fuzzyTool(name){
 name=(name||"").toLowerCase().trim();let best=null,score=0;
 TOOLS.forEach(t=>{const lb=t.b.toLowerCase();let sc=lb===name?100:(lb.includes(name)||name.includes(lb))?50+Math.min(lb.length,name.length):0;if(sc>score){score=sc;best=t}});
 return score>=20?best:null;
}
function fuzzyFolder(name){
 name=(name||"").toLowerCase().trim();
 return menuGroups().find(g=>g.name.toLowerCase()===name)||menuGroups().find(g=>g.name.toLowerCase().includes(name)||name.includes(g.name.toLowerCase()));
}
function menuCommand(text){
 let m;
 if((m=text.match(/^(?:please |can you |could you |hey atlas,? )*(?:create|make|add)\s+(?:(?:a|an|new)\s+)*folder\s+(?:called|named)?\s*["']?([^"'.!?]{1,40}?)["']?\s*(?:in|inside|for)?\s*(?:the\s+)?(?:tools?\s+)?(?:menu)?\.?$/i))){
  const name=m[1].trim();if(!name)return null;
  if(menuGroups().some(g=>g.name.toLowerCase()===name.toLowerCase()))return 'There\u2019s already a folder called "'+name+'" in the tools menu.';
  menuGroups().push({id:"g"+Date.now(),name,tools:[]});mSave();
  return 'Done \u2014 I created a folder called "'+name+'" in the tools menu. Open the hamburger menu to see it, or go to Settings \u2192 Customize tools menu to drag tools into it.';
 }
 if((m=text.match(/^(?:please |can you |could you )*rename\s+(?:the\s+)?folder\s+["']?([^"'.!?]{1,40})["']?\s+to\s+["']?([^"'.!?]{1,40})["']?\.?$/i))){
  const g=fuzzyFolder(m[1]);if(!g)return "I couldn't find a tools-menu folder called \""+m[1].trim()+'".';
  g.name=m[2].trim();mSave();return 'Done \u2014 renamed it to "'+g.name+'".';
 }
 if((m=text.match(/^(?:please |can you |could you )*(?:delete|remove)\s+(?:the\s+)?folder\s+["']?([^"'.!?]{1,40})["']?\.?$/i))){
  const g=fuzzyFolder(m[1]);if(!g)return "I couldn't find a tools-menu folder called \""+m[1].trim()+'".';
  S.menuGroups=menuGroups().filter(x=>x!==g);mSave();
  return 'Done \u2014 deleted the "'+g.name+'" folder. Its tools moved back to the unsorted part of the menu.';
 }
 if((m=text.match(/^(?:please |can you |could you )*(?:move|put)\s+(?:the\s+)?["']?([^"']{1,40}?)["']?\s+(?:tool\s+|button\s+)?(?:into|in|to)\s+(?:the\s+)?(?:folder\s+["']?([^"'.!?]{1,40})["']?|["']?([^"'.!?]{1,40}?)["']?\s+folder)\.?$/i))){
  const t=fuzzyTool(m[1]),fname=m[2]||m[3],g=fuzzyFolder(fname);
  if(!t)return "I'm not sure which tool you mean by \""+m[1].trim()+'".';
  if(!g)return "I couldn't find a tools-menu folder called \""+(fname||"").trim()+'". Ask me to create it first.';
  menuGroups().forEach(x=>{x.tools=x.tools.filter(id=>id!==t.id)});g.tools.push(t.id);mSave();
  return 'Done \u2014 moved "'+t.b+'" into "'+g.name+'".';
 }
 if((m=text.match(/^(?:please |can you |could you )*(?:move|put)\s+(?:the\s+)?["']?([^"']{1,40}?)["']?\s+(?:tool\s+|button\s+)?(?:into|in|to)\s+(?:the\s+)?["']?([^"'.!?]{1,40}?)["']?\.?$/i))){
  const t=fuzzyTool(m[1]),g=fuzzyFolder(m[2]);
  if(t&&g){menuGroups().forEach(x=>{x.tools=x.tools.filter(id=>id!==t.id)});g.tools.push(t.id);mSave();return 'Done \u2014 moved "'+t.b+'" into "'+g.name+'".';}
  return null;
 }
 return null;
}

let ICOIMG=null;
function icsay(t){$("icmsg").textContent=t||""}
function setFavicon(v){
 document.querySelectorAll('link[rel="icon"]').forEach(l=>l.remove());
 const l=document.createElement("link");l.rel="icon";l.type="image/png";l.href="/icon.png?v="+v;document.head.append(l);$("icprev").src=l.href;
}
function rr(x,s,r){x.moveTo(r,0);x.arcTo(s,0,s,s,r);x.arcTo(s,s,0,s,r);x.arcTo(0,s,0,0,r);x.arcTo(0,0,s,0,r);x.closePath()}
function iconCanvas(size){
 const cv=document.createElement("canvas");cv.width=cv.height=size;
 const x=cv.getContext("2d"),sh=$("icshape").value,r=sh==="circle"?size/2:sh==="rounded"?size*0.2237:0;
 x.imageSmoothingQuality="high";x.beginPath();if(r>0)rr(x,size,r);else x.rect(0,0,size,size);x.clip();
 if(!$("ictrans").checked){x.fillStyle=$("icbg").value;x.fillRect(0,0,size,size)}
 const w=ICOIMG.naturalWidth||ICOIMG.width,h=ICOIMG.naturalHeight||ICOIMG.height;
 const sc=$("icfit").value==="fill"?Math.max(size/w,size/h):Math.min(size/w,size/h)*0.9;
 x.drawImage(ICOIMG,(size-w*sc)/2,(size-h*sc)/2,w*sc,h*sc);
 return cv;
}
function scaled(src,size){
 let c=src;
 while(c.width/2>=size){const t=document.createElement("canvas");t.width=t.height=c.width/2;const x=t.getContext("2d");x.imageSmoothingQuality="high";x.drawImage(c,0,0,t.width,t.height);c=t}
 const o=document.createElement("canvas");o.width=o.height=size;const x=o.getContext("2d");x.imageSmoothingQuality="high";x.drawImage(c,0,0,size,size);return o;
}
function icPreview(){if(ICOIMG)$("icprev").src=iconCanvas(128).toDataURL()}
function applyTheme(){const t=S.theme||"";document.documentElement.dataset.theme=t;try{localStorage.setItem("atlasTheme",t)}catch(e){}}

async function send(text){
 if(busy){ctl&&ctl.abort();return}
 text=(text||"").trim();
 if(!text&&!(PA.imgs.length||PA.notes.length))return;
 if(text&&!(PA.imgs.length||PA.notes.length)){
  const mr=menuCommand(text);
  if(mr){
   $("input").value="";autosize();
   const c=cur()||newChat();
   c.messages.push({role:"user",content:text},{role:"assistant",content:mr});
   if(c.messages.length===2)c.title=text.slice(0,40);
   save();render();return;
  }
 }
 if(!text)text=PA.notes.length?"What happens in this video?":"What is in this image?";
 if(/^(please |can you |could you )?(upload|push|publish)\b.*\bgithub\b/i.test(text)){$("input").value="";autosize();openGH();return}
 if(/^(please |can you |could you )?convert\b.*\b(videos?|clips?|images?|photos?|pictures?|files?|mp4|png|jpe?g|gif|webp)\b/i.test(text)){$("input").value="";autosize();openConvert();return}
 if(/^(please |can you |could you )?(share|send|transfer)\b.*\b(phone|mobile)\b/i.test(text)){$("input").value="";autosize();openShare();return}
 if(/^(please |can you |could you )?(sort|organi[sz]e|file)\b.*\b(videos?|clips?|recordings?|images?|photos?|pictures?)\b/i.test(text)){$("input").value="";autosize();openVideos();return}
 if(/\b(organi[sz]e|tidy|clean up|sort)\b.*\b(downloads|desktop|documents|folder)\b/i.test(text)){
  const m=text.match(/downloads|desktop|documents/i);
  $("input").value="";autosize();openTask(m?"~/"+m[0][0].toUpperCase()+m[0].slice(1).toLowerCase():"~/Downloads");return;
 }
 if(!S.model){await loadModels();if(!S.model)return}
 const c=cur()||newChat();
 const um={role:"user",content:text};if(PA.imgs.length)um.images=PA.imgs;if(PA.notes.length)um.ctx=PA.notes.join("\n\n");
 c.messages.push(um);PA={imgs:[],notes:[],labels:[]};renderPA();
 if(c.messages.length===1)c.title=text.slice(0,40);
 const a={role:"assistant",content:""};c.messages.push(a);
 $("input").value="";autosize();status("");
 busy=true;ctl=new AbortController();render();
 const history=c.messages.slice(0,-1);
 try{
  const hits=retrieve(text);
  const hasImg=history.slice(-13).some(x=>x.images&&x.images.length);
  const model=hasImg&&!isVision(S.model)?visionModel():S.model;
  if(!model)throw new Error(NOVISION);
  const q=hits.length?"Reference material (use it, and say if it does not cover the question):\n"+hits.map(h=>"["+h[1]+"] "+h[2]).join("\n---\n")+"\n\nQuestion: "+mc(history[history.length-1]):mc(history[history.length-1]);
  const r=await fetch("/api/chat",{method:"POST",headers:H,signal:ctl.signal,body:JSON.stringify({
   model:model,stream:true,keep_alive:"30m",options:{num_ctx:8192},messages:[{role:"system",content:sys()},...history.slice(-13,-1).map(x=>({role:x.role,content:mc(x),images:x.images})),{role:"user",content:q,images:history[history.length-1].images}]})});
  if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||"Ollama returned error "+r.status)}
  const rd=r.body.getReader(),dec=new TextDecoder();let buf="";
  const el=()=>$("list").lastElementChild;
  while(true){
   const {done,value}=await rd.read();if(done)break;
   buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf("\n"))>=0){
    const line=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!line)continue;
    const j=JSON.parse(line);if(j.error)throw new Error(j.error);
    if(j.message){a.content+=j.message.content;el().innerHTML=md(a.content);$("msgs").scrollTop=$("msgs").scrollHeight}
    if(j.done&&j.eval_duration)status((j.eval_count/(j.eval_duration/1e9)).toFixed(1)+" tokens per second",true);
   }
  }
 }catch(e){if(e.name!=="AbortError")status(e.message)}
 if(!a.content)c.messages.pop();
 if(!c.topic&&c.messages.length>=2)sortChat(c).then(()=>{save();renderChats()});
 busy=false;ctl=null;save();render();
}

function autosize(){const t=$("input");t.style.height="auto";t.style.height=Math.min(t.scrollHeight,200)+"px"}
$("input").addEventListener("input",autosize);
$("input").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send($("input").value)}});
$("send").onclick=()=>send($("input").value);
$("new").onclick=()=>{document.body.classList.remove("open");newChat()};
$("menu").onclick=()=>document.body.classList.toggle("open");
$("model").onchange=e=>{S.model=e.target.value;save();api("/api/warm",{model:S.model}).catch(()=>{})};
$("model").addEventListener("focus",loadModels);
$("system").oninput=e=>{S.system=e.target.value;save()};
$("preset").onchange=e=>{S.preset=+e.target.value;S.system=PRESETS[S.preset].text;$("system").value=S.system;save()};

$("mode").onchange=e=>{S.mode=e.target.value;$("gamebox").hidden=S.mode!=="game";save();render()};
$("game").oninput=e=>{S.game=e.target.value;save()};
$("notes").oninput=e=>{S.notes=e.target.value;save()};
$("addfile").onclick=()=>$("file").click();
$("file").onchange=e=>{addFiles([...e.target.files]);e.target.value=""};
$("addurl").onclick=async()=>{
 const u=prompt("Paste a link to a guide or wiki page");if(!u)return;
 try{status("Fetching page\u2026",true);const j=await api("/api/url",{url:u});D.push({name:u,text:j.text,on:true});status("");saveDocs();renderDocs()}catch(e){status(e.message)}
};
addEventListener("dragover",e=>e.preventDefault());
addEventListener("drop",e=>{e.preventDefault();const fs=[...e.dataTransfer.files],media=f=>/^(image|video)\//.test(f.type);attach(fs.filter(media));const docs=fs.filter(f=>!media(f));if(docs.length)addFiles(docs)});
$("tidy").onclick=()=>openTask("~/Downloads");
$("attach").onclick=()=>$("afile").click();
$("afile").onchange=e=>{attach([...e.target.files]);e.target.value=""};
$("input").addEventListener("paste",e=>{const fs=[...e.clipboardData.files].filter(f=>f.type.startsWith("image/"));if(fs.length){e.preventDefault();attach(fs)}});
$("sharebtn").onclick=openShare;
$("shclose").onclick=()=>$("shdlg").close();
$("shstart").onclick=async()=>{try{$("shmsg").textContent="";shRender(await api("/api/share",{action:"start",recv:$("shrecv").value}))}catch(e){$("shmsg").textContent=e.message}};
$("shstop").onclick=async()=>{try{shRender(await api("/api/share",{action:"stop"}))}catch(e){$("shmsg").textContent=e.message}};
$("shopen").onclick=()=>api("/api/shareopen",{}).catch(e=>{$("shmsg").textContent=e.message});
$("shadd").onclick=()=>$("shfile").click();
$("shfile").onchange=async e=>{
 for(const f of [...e.target.files]){
  try{$("shmsg").textContent="Adding "+f.name+"\u2026";shRender(await api("/api/shareadd",f,f.name))}catch(x){$("shmsg").textContent=x.message;e.target.value="";return}
 }
 $("shmsg").textContent="";e.target.value="";
};
function toolMenu(open){
 $("toolmenu").hidden=!open;$("toolsbtn").setAttribute("aria-expanded",open);
 if(open)$("toolmenu").querySelector("button").focus();
}
function openSettings(){loadModels();ollCheck(false);$("setdlg").showModal()}
$("toolsbtn").onclick=e=>{e.stopPropagation();toolMenu($("toolmenu").hidden)};
$("toolmenu").addEventListener("click",e=>{if(e.target.closest("button")&&!e.target.closest(".mgroup"))toolMenu(false)});
document.addEventListener("click",e=>{if(!e.target.closest("#toolmenu,#toolsbtn"))toolMenu(false)});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!$("toolmenu").hidden){toolMenu(false);$("toolsbtn").focus()}});
$("gearbtn").onclick=openSettings;$("modelchip").onclick=openSettings;
$("mopenbtn").onclick=openMenuEdit;
$("mdone").onclick=()=>$("menudlg").close();
$("mnewgo").onclick=()=>{
 const v=$("mnewname").value.trim();if(!v){$("mmsg").textContent="Type a name first.";return}
 if(menuGroups().some(g=>g.name.toLowerCase()===v.toLowerCase())){$("mmsg").textContent='A folder called "'+v+'" already exists.';return}
 menuGroups().push({id:"g"+Date.now(),name:v,tools:[]});$("mnewname").value="";$("mmsg").textContent="";mSave();mRenderFolders();
};
$("mnewname").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();$("mnewgo").click()}};
$("setclose").onclick=()=>$("setdlg").close();
$("ollcheck").onclick=()=>ollCheck(false);
$("vtbtn").onclick=visionTest;
$("codebtn").onclick=()=>{$("codedlg").showModal();if($("codefolder").value&&!CODEF.length)codeOpen()};
$("cyberbtn").onclick=cyOpen;
$("cyclose").onclick=()=>{CYDRILL=false;$("cyberdlg").close()};
$("cyback").onclick=cyExit;
$("cydrill").onclick=cyDrillStart;
$("codeclose").onclick=()=>$("codedlg").close();
$("codeopen").onclick=codeOpen;
$("codego").onclick=codeAsk;
$("codeimg").onclick=()=>$("codeimgfile").click();
$("codeimgfile").onchange=async e=>{try{for(const f of [...e.target.files])CODEIMGS.push(await shrink(f));renderCodeImgs()}catch(x){codesay(x.message)}e.target.value=""};
$("codeundo").onclick=async()=>{try{const j=await api("/api/code",{action:"undo"});codesay(j.restored?"Put back "+j.restored+" file"+(j.restored===1?"":"s")+" as they were.":"Nothing to undo.");$("codeout").innerHTML="";$("codeapply").hidden=true;if(j.restored)codeOpen("Put back "+j.restored+" file"+(j.restored===1?"":"s")+" as they were.")}catch(e){codesay(e.message)}};
$("codeapply").onclick=async()=>{
 const sel=[...$("codeout").querySelectorAll(".cchg input:checked")].map(x=>CODECHG[+x.dataset.i]).map(c=>({path:c.path,content:c.content,expect_new:!c.existing}));
 if(!sel.length){codesay("Tick at least one change.");return}
 try{const j=await api("/api/code",{action:"apply",folder:$("codefolder").value,changes:sel});$("codeapply").hidden=true;codeOpen("Wrote "+j.written+" file"+(j.written===1?"":"s")+". Undo last apply puts everything back.")}
 catch(e){codesay(e.message)}
};
$("autobtn").onclick=openAuto;
$("autoclose").onclick=()=>$("autodlg").close();
$("autopos").onclick=async()=>{
 for(let n=3;n>0;n--){$("autoposout").textContent="Put your mouse where you want it. Reading in "+n+"\u2026";await new Promise(r=>setTimeout(r,1000))}
 try{const j=await api("/api/auto",{action:"pos"});$("autoposout").textContent="Your mouse is at ("+j.x+", "+j.y+"). I added that to your task.";$("autotask").value=($("autotask").value+" (position "+j.x+", "+j.y+")").trim()}
 catch(e){$("autoposout").textContent=e.message}
};
$("automake").onclick=async()=>{
 const task=$("autotask").value.trim();if(!task){$("autostat").textContent="Describe the task first.";return}
 try{
  $("autostat").textContent="Thinking\u2026";$("autoplan").hidden=true;
  const inf=await api("/api/auto",{action:"info"});let img=null,model=S.model,k=[1,1],w=inf.w,h=inf.h;
  if($("autoshot").checked){const sh=await api("/api/auto",{action:"shot"});img=sh.b64;model=visionModel();if(!model)throw new Error(NOVISION);k=[inf.w/sh.w,inf.h/sh.h];w=sh.w;h=sh.h}
  const out=await ask([{role:"system",content:autoSys(w,h,!!img)},{role:"user",content:task,images:img?[img]:undefined}],{temperature:0,num_ctx:8192,num_predict:1500},model);
  const m=out.match(/\{[\s\S]*\}/);let plan;
  try{plan=JSON.parse(m[0])}catch(e){throw new Error("The AI didn't give usable steps. Try describing the task more simply, or write the steps yourself under Advanced.")}
  if(plan.error)throw new Error(plan.error);
  if(img)(plan.steps||[]).forEach(st=>{["x","x2"].forEach(a=>{if(typeof st[a]==="number")st[a]=Math.round(st[a]*k[0])});["y","y2"].forEach(a=>{if(typeof st[a]==="number")st[a]=Math.round(st[a]*k[1])})});
  await autoShow(plan);$("autostat").textContent="Check the steps above. Nothing happens until you click Run.";
 }catch(e){$("autostat").textContent=fixErr(e.message)}
};
$("autocheck").onclick=async()=>{try{await autoShow(JSON.parse($("autojson").value));$("autostat").textContent="Steps updated."}catch(e){$("autostat").textContent=e.message}};
$("autorun").onclick=async()=>{
 try{await api("/api/auto",{action:"run",plan:AUTOPLAN,delay:+$("autodelay").value});$("autostop").hidden=false;$("autorun").disabled=true;autoPoll()}
 catch(e){$("autostat").textContent=e.message}
};
$("autostop").onclick=()=>api("/api/auto",{action:"stop"}).catch(()=>{});
$("olldl").onclick=()=>api("/api/ollama",{action:"download"}).catch(e=>{$("ollinfo").textContent=e.message});
$("ollauto").addEventListener("change",()=>{if($("ollauto").checked)ollCheck(true)});
$("theme").onchange=e=>{S.theme=e.target.value;applyTheme();save()};
$("icpick").onclick=()=>$("icfile").click();
["icfit","icshape","icbg","ictrans"].forEach(id=>$(id).addEventListener("input",icPreview));
$("icfile").onchange=async e=>{
 const f=e.target.files[0];e.target.value="";if(!f)return;
 try{
  const im=await new Promise((res,rej)=>{const i=new Image(),u=URL.createObjectURL(f);i.onload=()=>{URL.revokeObjectURL(u);res(i)};i.onerror=()=>rej(new Error("Couldn't read that image."));i.src=u});
  if(!im.naturalWidth)throw new Error("Couldn't read that image.");
  const m=Math.max(im.naturalWidth,im.naturalHeight);let src=im;
  if(m>1536){const k=1536/m,c=document.createElement("canvas");c.width=Math.round(im.naturalWidth*k);c.height=Math.round(im.naturalHeight*k);const x=c.getContext("2d");x.imageSmoothingQuality="high";x.drawImage(im,0,0,c.width,c.height);src=c}
  ICOIMG=src;$("icopts").hidden=false;icPreview();icsay("Looks right? Adjust the options if you like, then click Save icon.");
 }catch(err){icsay(err.message)}
};
$("icsave").onclick=async()=>{
 if(!ICOIMG)return;
 try{
  icsay("Saving\u2026");
  const master=iconCanvas(512),sizes={};
  [16,24,32,48,64,128,256,512].forEach(n=>{sizes[n]=(n===512?master:scaled(master,n)).toDataURL("image/png").split(",")[1]});
  const j=await api("/api/icon",{action:"save",sizes});
  ICOIMG=null;$("icopts").hidden=true;setFavicon(j.v);
  icsay("Saved. To use it on your desktop shortcut too, click Update my desktop shortcut.");
 }catch(err){icsay(err.message)}
};
$("icshort").onclick=async()=>{try{await api("/api/icon",{action:"shortcut"});icsay("Done. If you see an old shortcut next to the new one, delete the old one. Windows can take a moment to show the new picture.")}catch(err){icsay(err.message)}};
$("icreset").onclick=async()=>{try{await api("/api/icon",{action:"reset"});location.reload()}catch(err){icsay(err.message)}};
$("voicesel").onchange=e=>{S.voice=e.target.value;save()};
$("vrate").oninput=e=>{S.rate=+e.target.value;vlabels();save()};
$("vpitch").oninput=e=>{S.pitch=+e.target.value;vlabels();save()};
$("vprev").onclick=()=>speak("Hi! This is how I sound. Pick the voice you like best.");
$("docsbtn").onclick=()=>$("docdlg").showModal();$("docclose").onclick=()=>$("docdlg").close();
$("vids").onclick=openVideos;$("gh").onclick=openGH;
$("vclose").onclick=()=>$("vdlg").close();
$("vvprev").onclick=()=>vvShow(VI-1);$("vvnext").onclick=()=>vvShow(VI+1);$("vvclose").onclick=vvClose;
$("vvcb").onchange=()=>{const v=VF[VI];v.sel=$("vvcb").checked;vmark(v)};
$("vview").addEventListener("keydown",e=>{
 if(e.target.tagName==="INPUT"&&e.target.type==="text"){if(e.key==="Enter"){e.preventDefault();(e.target.id==="vvrname"?$("vvrnok"):$("vvpmk")).click()}else if(e.key==="Escape"){e.preventDefault();$("vvpick").hidden=true;$("vvrn").hidden=true;$("vview").focus()}return}
 if(e.key==="ArrowRight"){e.preventDefault();vvShow(VI+1)}
 else if(e.key==="ArrowLeft"){e.preventDefault();vvShow(VI-1)}
 else if(e.key==="s"||e.key==="S"){e.preventDefault();vvToggle()}
 else if(e.key==="Escape"&&!document.fullscreenElement){e.preventDefault();vvClose()}
});
document.addEventListener("fullscreenchange",()=>{if(!document.fullscreenElement&&!$("vview").hidden)vvHide()});
$("vdlg").addEventListener("cancel",e=>{if(!$("vview").hidden){e.preventDefault();vvClose()}else if(FNEW){e.preventDefault();FNEW=false;fRender()}else if(document.querySelector(".vedit")){e.preventDefault()}});$("ghclose").onclick=()=>$("ghdlg").close();
$("vload").onclick=()=>vLoad();
$("fnew").onclick=()=>{FNEW=true;fRender()};
$("fup").onclick=()=>{if(VUP)openFolder(VUP)};
$("fdel").onclick=async()=>{
 let ok=0,skip=0;
 for(const n of [...FSEL]){try{await api("/api/folder",{action:"delete",folder:VCUR,name:n});ok++}catch(e){skip++}}
 await vLoad("Deleted "+ok+" empty folder"+(ok===1?"":"s")+(skip?". Skipped "+skip+" that still have files in them.":"."));
};
document.addEventListener("click",e=>{if(!e.target.closest("#fmenu,.fdots"))$("fmenu").hidden=true});
$("vvren").onclick=()=>{
 if(!$("vvrn").hidden){$("vvrn").hidden=true;return}
 const v=VF[VI];if(!v)return;const dot=v.name.lastIndexOf(".");
 $("vvrname").value=v.name.slice(0,dot);$("vvrnext").textContent=v.name.slice(dot);$("vvrnmsg").textContent="";
 $("vvpick").hidden=true;$("vvrn").hidden=false;$("vvrname").focus();$("vvrname").select();
};
$("vvrnok").onclick=vvRename;
$("vvadd").onclick=()=>{$("vvrn").hidden=true;if($("vvpick").hidden){renderPick();$("vvpname").value="";$("vvpick").hidden=false}else $("vvpick").hidden=true};
$("vvpmk").onclick=async()=>{
 const n=$("vvpname").value.trim();if(!n){$("vvpmsg").textContent="Type a folder name first.";return}
 try{const f=await api("/api/folder",{action:"create",folder:VCUR,name:n});await vvAddTo(f)}catch(e){$("vvpmsg").textContent=e.message}
};
$("vall").onclick=()=>{VF.forEach(v=>v.sel=true);vRender();vcount()};
$("vnone").onclick=()=>{VF.forEach(v=>v.sel=false);vRender();vcount()};
$("vgo").onclick=async()=>{
 const files=VF.filter(v=>v.sel).map(v=>v.path);
 if(!files.length){vsay("Tick at least one video first.");return}
 if(!$("vdest").value.trim()){vsay("Type the folder to send them to.");return}
 try{vsay("Working\u2026 large videos can take a while.");const j=await api("/api/send",{files,dest:$("vdest").value,mode:$("vmode").value});
  vsay("Sent "+j.n+" videos to "+j.dest+". Undo last run reverses it.");VF.forEach(v=>v.sel=false);
  if($("vmode").value==="move")$("vload").click();else vRender()}
 catch(e){vsay(e.message)}
};
$("convbtn").onclick=openConvert;
$("cclose").onclick=()=>$("cvdlg").close();
$("cload").onclick=async()=>{
 try{csay("Loading\u2026");const j=await api("/api/videos",{folder:$("cfolder").value,deep:$("cdeep").checked});CF=j.videos;cRender();
  csay(CF.length?CF.length+" files. Tick the ones to convert.":"No videos or images found there.")}
 catch(e){csay(e.message)}
};
$("call").onclick=()=>{CF.forEach(v=>v.sel=true);cRender();ccount()};
$("cnone").onclick=()=>{CF.forEach(v=>v.sel=false);cRender();ccount()};
$("cgo").onclick=async()=>{
 const sel=CF.filter(v=>v.sel),fmt=$("cfmt").value.trim().replace(/^\./,"").toLowerCase();
 if(!sel.length){csay("Tick at least one file first.");return}
 if(!/^[a-z0-9]{2,5}$/.test(fmt)){csay("Type a format like mp4, webp or mp3.");return}
 let ok=0;const errs=[];
 for(let i=0;i<sel.length;i++){
  csay("Converting "+sel[i].name+" ("+(i+1)+" of "+sel.length+")\u2026 big videos can take a while.");
  try{await api("/api/convert",{path:sel[i].path,fmt,dest:$("cdest").value});ok++}catch(e){errs.push(sel[i].name+": "+e.message);if(/needs ffmpeg/.test(e.message))break}
 }
 csay("Converted "+ok+" of "+sel.length+(errs.length?". Problems: "+errs.join(" | "):". They are in the Converted folder next to the originals, or the folder you chose."));
};
$("vundo").onclick=async()=>{try{const j=await api("/api/undo");await vLoad(j.n?"Undid "+j.n+" file"+(j.n===1?"":"s")+".":"Nothing to undo. The files may have moved again, or their folder was renamed.")}catch(e){vsay(e.message)}};
document.addEventListener("click",e=>{const a=e.target.closest("a[data-ext]");if(a){e.preventDefault();api("/api/openurl",{url:a.href}).catch(()=>{})}});
$("ghtest").onclick=async()=>{
 const L=$("ghlist");
 try{
  if($("ghtoken").value.trim()){await api("/api/ghtoken",{token:$("ghtoken").value.trim()});$("ghtoken").value="";$("ghhas").textContent="(saved)"}
  L.textContent="Testing\u2026";
  const j=await api("/api/ghcheck",{repo:$("ghrepo").value.trim()});
  L.innerHTML=j.lines.map(x=>{const i=x.indexOf("|"),k=x.slice(0,i);return "<div>"+({ok:"\u2713 ",bad:"\u2717 ",warn:"! "}[k]||"")+esc(x.slice(i+1))+"</div>"}).join("");
 }catch(e){L.textContent=e.message}
};
$("ghprev").onclick=async()=>{
 const L=$("ghlist");
 try{
  if($("ghtoken").value.trim()){await api("/api/ghtoken",{token:$("ghtoken").value.trim()});$("ghtoken").value=""}
  const j=await api("/api/ghplan",{folder:$("ghfolder").value});
  $("ghhas").textContent=j.has_token?"(saved)":"(none saved yet)";
  L.innerHTML="<strong>"+j.files.length+" files will upload</strong>"+(j.more?" (the first 300 only)":"")+":<br>"+esc(j.files.slice(0,60).join(", "))+(j.files.length>60?" \u2026":"")+(j.skipped.length?"<br><br><strong>Skipped ("+j.skipped.length+"):</strong> "+esc(j.skipped.slice(0,30).map(s=>s[0]+" ("+s[1]+")").join(", ")):"")+(j.has_token?"":"<br><br>Paste a token above and click Preview again.");
  $("ghgo").hidden=!j.files.length||!j.has_token;
 }catch(e){L.textContent=e.message}
};
$("ghgo").onclick=async()=>{
 const L=$("ghlist");
 try{
  L.textContent="Uploading\u2026 this can take a minute.";$("ghgo").hidden=true;
  const j=await api("/api/ghgo",{folder:$("ghfolder").value,repo:$("ghrepo").value,create:$("ghnew").checked,message:$("ghmsg").value});
  L.innerHTML='Done! <a href="'+esc(j.url)+'" data-ext style="color:var(--accent)">'+esc(j.url)+"</a>";
 }catch(e){L.textContent=e.message;$("ghgo").hidden=false}
};
$("ghsug").onclick=async()=>{
 try{
  const j=await api("/api/ghplan",{folder:$("ghfolder").value});
  const t=await ask([{role:"system",content:"Write a git commit message of at most 10 words for uploading these files. Output only the message."},{role:"user",content:j.files.slice(0,40).join("\n")}],{num_predict:30});
  $("ghmsg").value=t.replace(/["\n]/g,"").slice(0,80);
 }catch(e){$("ghlist").textContent=e.message}
};
$("ghforget").onclick=async()=>{try{await api("/api/ghtoken",{token:""});$("ghhas").textContent="(none saved yet)"}catch(e){$("ghlist").textContent=e.message}};
$("q").oninput=e=>{Q=e.target.value;renderChats()};
$("sortall").onclick=async()=>{
 const todo=S.chats.filter(c=>c.messages.length>=2&&(!c.topic||c.topic==="General"));
 if(!todo.length){status("All your chats are already sorted.",true);return}
 for(let i=0;i<todo.length;i++){status("Sorting chat "+(i+1)+" of "+todo.length+"\u2026",true);delete todo[i].topic;await sortChat(todo[i]);renderChats()}
 status("");save();
};
$("voicesample").oninput=e=>{S.voiceSample=e.target.value;save()};
$("vbtn").onclick=()=>{
 if(V){stopVoice();return}
 if(!SR){status("Voice mode needs Chrome or Edge.");return}
 V=true;vbtnState(true);listen();
};
$("makegame").onclick=()=>{$("maker").hidden=false;document.body.classList.remove("open");mkShow();mkMsgs()};
$("gclose").onclick=()=>{$("maker").hidden=true};
$("gsend").onclick=()=>mkSend($("ginput").value);
$("ginput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();mkSend($("ginput").value)}});
$("gundo").onclick=()=>{const m=MK();if(m.ver.length){m.code=m.ver.pop();m.msgs.push({role:"assistant",text:"Went back one version."});mkShow();mkMsgs();save()}};
$("gview").onclick=()=>{$("gcode").hidden=!$("gcode").hidden;$("grun").hidden=$("gcode").hidden};
$("grun").onclick=()=>{const m=MK();m.ver.push(m.code);m.code=$("gcode").value;mkShow();save()};
$("gdl").onclick=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([MK().code],{type:"text/html"}));a.download="my-game.html";a.click()};
$("gnew").onclick=()=>{if(confirm("Start a new game? The current one will be cleared.")){S.maker={code:"",ver:[],msgs:[]};mkShow();mkMsgs();save()}};
$("gfix").onclick=()=>mkSend("The game shows this error, please fix it: "+$("gerrt").textContent);
addEventListener("message",e=>{if(e.source===$("gframe").contentWindow&&e.data&&e.data.gameError){
 const er=e.data.gameError;$("gerrt").textContent=er;$("gerr").hidden=false;
 if(gfixes<2&&!gbusy){gfixes++;mkSend("The game shows this error, please fix it: "+er,true)}
}});
$("close").onclick=()=>$("task").close();
$("prev").onclick=async()=>{
 try{
  P=(await api("/api/plan",{folder:$("folder").value,days:+$("days").value||90})).moves;const by={};
  P.forEach(m=>{const k=m.dst.split(/[\\/]/).slice(-2)[0];(by[k]=by[k]||[]).push(m.src.split(/[\\/]/).pop()+(m.why?" ("+m.why+")":""))});
  $("plan").innerHTML=P.length?Object.entries(by).map(([k,v])=>"<strong>"+esc(k)+"</strong> ("+v.length+"): "+esc(v.join(", "))).join("<br><br>"):"Nothing to move.";
  $("go").hidden=!P.length;
 }catch(e){$("plan").textContent=e.message}
};
$("go").onclick=async()=>{
 try{const j=await api("/api/run",{folder:$("folder").value,moves:P});$("plan").textContent="Moved "+j.n+" files. Use Undo last run to put them back.";$("go").hidden=true}catch(e){$("plan").textContent=e.message}
};
$("undo").onclick=async()=>{try{const j=await api("/api/undo");$("plan").textContent="Put "+j.n+" files back."}catch(e){$("plan").textContent=e.message}};

(async()=>{
 PRESETS.forEach((p,i)=>{const o=document.createElement("option");o.value=i;o.textContent=p.name;$("preset").append(o)});
 menuRender();
 document.querySelectorAll("dialog").forEach(d=>{
  const x=document.createElement("button");x.type="button";x.className="xclose";x.setAttribute("aria-label","Close");
  x.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>';
  x.onclick=()=>{
   if(d.id==="vdlg"){
    if(!$("vview").hidden){vvClose();return}
    if(FNEW){FNEW=false;fRender();return}
   }
   d.close();
  };
  d.prepend(x);
 });
 try{const r=await fetch("/api/state");Object.assign(S,await r.json())}catch(e){}
 $("system").value=S.system;$("preset").value=S.preset;$("voicesample").value=S.voiceSample||"";fetch("/api/ping").catch(()=>{});setInterval(()=>fetch("/api/ping").catch(()=>{}),15000);$("mode").value=S.mode;$("game").value=S.game;$("notes").value=S.notes;$("gamebox").hidden=S.mode!=="game";
 ["vfolder","vdest","vmode","vdeep","cfolder","cdeep","cfmt","cdest","ghfolder","ghrepo","ollauto","codefolder"].forEach(k=>remember(k,k));
 if(S.ollauto)setTimeout(()=>ollCheck(true),4000);
 $("theme").value=S.theme||"";applyTheme();$("icprev").src=document.querySelector('link[rel="icon"][type="image/png"]').href;
 $("vrate").value=S.rate||1;$("vpitch").value=S.pitch||1;vlabels();
 if(window.speechSynthesis){speechSynthesis.onvoiceschanged=fillVoices;fillVoices()}
 try{const r=await fetch("/api/docs");D=await r.json();renderDocs()}catch(e){}
 render();await loadModels();if(S.model)api("/api/warm",{model:S.model}).catch(()=>{});
})();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")

    def tool(self, path, raw):
        if path == "/api/extract":
            return {"text": extract(urllib.parse.unquote(self.headers.get("X-Name", "")), raw)}
        j = json.loads(raw or b"{}")
        if path == "/api/url":
            return {"text": fetch_url(j["url"])}
        if path == "/api/plan":
            return {"moves": make_plan(j["folder"], int(j.get("days") or 90))}
        if path == "/api/run":
            return {"n": run_moves(j["folder"], j["moves"], j.get("mode", "move"))}
        if path == "/api/videos":
            return list_videos(j["folder"], bool(j.get("deep")))
        if path == "/api/share":
            a = j.get("action")
            if a == "start":
                return share_start(j.get("recv", ""))
            if a == "stop":
                return share_stop()
            if a == "remove":
                v = SHARE["out"].pop(str(j.get("id")), None)
                if v and os.path.exists(v[0]):
                    os.remove(v[0])
            return share_state()
        if path == "/api/shareopen":
            open_folder(str(SHARE["recv"] or default_recv()))
            return {"ok": True}
        if path == "/api/icon":
            a = j.get("action")
            if a == "save":
                return save_icon(j.get("sizes", {}))
            if a == "reset":
                shutil.rmtree(ICONDIR, ignore_errors=True)
            if a == "shortcut":
                make_shortcut()
            return {"custom": os.path.exists(os.path.join(ICONDIR, "icon-256.png"))}
        if path == "/api/convert":
            return convert_file(j["path"], j["fmt"], j.get("dest", ""))
        if path == "/api/auto":
            return auto_api(j)
        if path == "/api/code":
            return code_api(j)
        if path == "/api/renamefile":
            return rename_file(j["path"], j.get("new", ""))
        if path == "/api/ollama":
            a = j.get("action")
            if a == "download":  # the official page picks the right installer for your computer
                webbrowser.open("https://ollama.com/download")
                return {"ok": True}
            if a == "status":
                return {"update": dict(UPD)}
            if a == "update":
                if os.name != "nt":
                    raise ValueError("Atlas can only install Ollama updates by itself on Windows. Use Download latest Ollama instead.")
                if UPD["state"] in ("downloading", "installing"):
                    return {"update": dict(UPD)}
                info = ollama_info()
                if not info["latest"]:
                    raise ValueError("Couldn't check the latest Ollama version. Are you online?")
                if info["newer"]:
                    UPD.update(state="downloading", pct=0, msg="Starting")
                    threading.Thread(target=win_update, args=(info["latest"],), daemon=True).start()
                return {"update": dict(UPD), "newer": info["newer"]}
            return ollama_info()
        if path == "/api/folder":
            return folder_op(j)
        if path == "/api/send":
            return send_files(j["files"], j["dest"], j.get("mode", "link"))
        if path == "/api/ghtoken":
            if "token" in j:
                if j["token"]:
                    fd = os.open(GH_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write(j["token"])
                elif os.path.exists(GH_TOKEN):
                    os.remove(GH_TOKEN)
            return {"saved": bool(gh_token())}
        if path == "/api/ghcheck":
            return {"lines": gh_check(j.get("repo", ""))}
        if path == "/api/openurl":  # opens GitHub pages in your normal browser, where you are signed in
            u = j.get("url", "")
            if not u.startswith("https://github.com/"):
                raise ValueError("That link isn't allowed.")
            webbrowser.open(u)
            return {"ok": True}
        if path == "/api/ghplan":
            _, ok, skip, more = gh_files(j["folder"])
            return {"files": ok, "skipped": skip, "more": more, "has_token": bool(gh_token())}
        if path == "/api/ghgo":
            return {"url": gh_upload(j["folder"], j["repo"].strip(), bool(j.get("create")), j.get("message", ""), j.get("prefix", ""))}
        if path == "/api/undo":
            return {"n": undo_moves()}
        if path == "/api/docs":
            with open(DOCS, "w") as fh:
                json.dump(j, fh)
            return {"ok": True}
        if path == "/api/warm":  # load the model into memory ahead of time
            body = json.dumps({"model": j["model"], "messages": [], "keep_alive": "30m"}).encode()
            urllib.request.urlopen(urllib.request.Request(
                OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"}), timeout=300).read()
            return {"ok": True}

    def serve_file(self):  # lets the page show video previews (supports seeking)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        p = Path(q.get("p", [""])[0])
        if q.get("t", [""])[0] != TOKEN or p.suffix.lower() not in MEDIA or not p.is_file():
            return self.reply(403, {"error": "Forbidden"})
        size = p.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            code = 206
        if start > end:
            return self.reply(416, {"error": "Bad range"})
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    b = fh.read(min(1 << 20, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
        except OSError:
            pass

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, {"error": "Forbidden"})
        if self.path.split("?")[0] == "/icon.png":
            p = os.path.join(ICONDIR, "icon-256.png")
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    return self.reply(200, fh.read(), "image/png")
            return self.reply(404, {"error": "No custom icon"})
        if self.path.startswith("/api/file?"):
            return self.serve_file()
        if self.path == "/":
            self.reply(200, page_html(), "text/html; charset=utf-8")
        elif self.path == "/api/caps":  # which installed models can look at images
            try:
                with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
                    names = [m["name"] for m in json.loads(r.read()).get("models", [])]
                for n in names:
                    if n not in CAPS:
                        try:
                            req = urllib.request.Request(OLLAMA + "/api/show", data=json.dumps({"model": n}).encode(),
                                                         headers={"Content-Type": "application/json"})
                            with urllib.request.urlopen(req, timeout=10) as r:
                                CAPS[n] = json.loads(r.read()).get("capabilities") or []
                        except Exception:
                            CAPS[n] = []
                self.reply(200, {n: CAPS.get(n, []) for n in names})
            except Exception:
                self.reply(200, {})
        elif self.path == "/api/ping":
            PING["t"], PING["seen"] = time.time(), True
            self.reply(200, {"ok": True})
        elif self.path == "/api/models":
            try:
                with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
                    self.reply(200, r.read())
            except Exception:
                self.reply(502, {"error": NO_OLLAMA})
        elif self.path == "/api/docs":
            try:
                with open(DOCS, "rb") as f:
                    self.reply(200, f.read())
            except Exception:
                self.reply(200, [])
        elif self.path == "/api/state":
            try:
                with open(DATA, "rb") as f:
                    self.reply(200, f.read())
            except Exception:
                self.reply(200, {})
        else:
            self.reply(404, {"error": "Not found"})

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Token") != TOKEN:
            return self.reply(403, {"error": "Forbidden"})
        if self.path == "/api/shareadd":  # streamed, because files can be large
            try:
                return self.reply(200, share_add(urllib.parse.unquote(self.headers.get("X-Name", "file")), self.rfile,
                                                 int(self.headers.get("Content-Length", 0))))
            except Exception as e:
                return self.reply(400, {"error": str(e)})
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/state":
            try:
                json.loads(raw)
                with open(DATA, "wb") as f:
                    f.write(raw)
                self.reply(200, {"ok": True})
            except Exception:
                self.reply(400, {"error": "Could not save"})
        elif self.path in ("/api/extract", "/api/url", "/api/plan", "/api/run", "/api/undo", "/api/warm", "/api/docs", "/api/videos", "/api/send", "/api/auto", "/api/code", "/api/renamefile", "/api/ollama", "/api/folder", "/api/convert", "/api/icon", "/api/share", "/api/shareopen", "/api/ghtoken", "/api/ghcheck", "/api/openurl", "/api/ghplan", "/api/ghgo"):
            try:
                self.reply(200, self.tool(self.path, raw))
            except Exception as e:
                self.reply(400, {"error": str(e)})
        elif self.path == "/api/chat":
            req = urllib.request.Request(OLLAMA + "/api/chat", data=raw,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for line in r:
                        self.wfile.write(line)
                        self.wfile.flush()
            except urllib.error.HTTPError as e:
                self.reply(e.code, e.read())
            except (BrokenPipeError, ConnectionResetError):
                pass  # you pressed Stop
            except Exception:
                try:
                    self.reply(502, {"error": NO_OLLAMA})
                except Exception:
                    pass
        else:
            self.reply(404, {"error": "Not found"})


# ---------- mouse and keyboard helper ----------
AUTO = {"state": "idle", "msg": "", "i": 0, "n": 0, "stop": False}


def _pg():
    try:
        import pyautogui
    except ImportError:
        raise ValueError("Mouse and keyboard control needs one extra install. In a terminal run:  pip install pyautogui  then restart Atlas.")
    except Exception as e:
        raise ValueError("Couldn't start mouse control on this computer: %s" % e)
    return pyautogui


def auto_clean(plan, size, keys):
    """Checks a plan from the AI (or from you) and returns (clean plan, plain-English lines)."""
    W, H = size

    def num(v, k):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            raise ValueError("Step %d has a number that isn't a number." % k)

    def pt(st, k, need, a="x", b="y"):
        if a not in st or b not in st:
            if need:
                raise ValueError("Step %d needs a position (x and y)." % k)
            return {}
        x, y = num(st[a], k), num(st[b], k)
        if not (0 <= x < W and 0 <= y < H):
            raise ValueError("Step %d points outside your screen, which is %d by %d." % (k, W, H))
        return {a: x, b: y}

    try:
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan["steps"]:
            raise ValueError("There are no steps to run.")
        rep = max(1, min(num(plan.get("repeat") or 1, 0), 100))
        steps, lines = [], []
        for k, st in enumerate(plan["steps"][:300], 1):
            if not isinstance(st, dict):
                raise ValueError("Step %d isn't valid." % k)
            op = str(st.get("op", "")).lower().replace("-", "_")
            s = {"op": op}
            if op == "move":
                s.update(pt(st, k, True))
                d = "Move the mouse to (%d, %d)" % (s["x"], s["y"])
            elif op in ("click", "double_click", "right_click"):
                s.update(pt(st, k, False))
                nm = {"click": "Click", "double_click": "Double-click", "right_click": "Right-click"}[op]
                d = "%s at (%d, %d)" % (nm, s["x"], s["y"]) if "x" in s else "%s where the mouse is now" % nm
            elif op == "drag":
                s.update(pt(st, k, True))
                s.update(pt(st, k, True, "x2", "y2"))
                d = "Drag from (%d, %d) to (%d, %d)" % (s["x"], s["y"], s["x2"], s["y2"])
            elif op == "scroll":
                s.update(pt(st, k, False))
                s["amount"] = max(-3000, min(3000, num(st.get("amount", 0), k)))
                d = "Scroll %s by %d" % ("up" if s["amount"] > 0 else "down", abs(s["amount"]))
            elif op == "type":
                s["text"] = str(st.get("text", ""))[:500]
                if not s["text"]:
                    raise ValueError("Step %d has nothing to type." % k)
                d = 'Type "%s"' % s["text"]
            elif op == "press":
                s["key"] = str(st.get("key", "")).lower()
                if s["key"] not in keys:
                    raise ValueError("Step %d: I don't know the key '%s'." % (k, s["key"]))
                d = "Press %s" % s["key"].title()
            elif op == "hotkey":
                s["keys"] = [str(x).lower() for x in st.get("keys", [])][:4]
                if len(s["keys"]) < 2 or any(x not in keys for x in s["keys"]):
                    raise ValueError("Step %d: a shortcut needs two to four keys I know, like ctrl and s." % k)
                d = "Press %s" % " + ".join(x.title() for x in s["keys"])
            elif op == "wait":
                s["seconds"] = max(0.0, min(float(st.get("seconds", 1)), 60.0))
                d = "Wait %g second%s" % (s["seconds"], "" if s["seconds"] == 1 else "s")
            else:
                raise ValueError("Step %d: I don't know the action '%s'." % (k, op))
            steps.append(s)
            lines.append(d)
        if len(steps) * rep > 3000:
            raise ValueError("That would be more than 3000 actions in total. Use fewer steps or repeats.")
    except (TypeError, KeyError, AttributeError) as e:
        raise ValueError("A step is missing something or has a wrong value (%s)." % e)
    return {"summary": str(plan.get("summary", ""))[:200], "repeat": rep, "steps": steps}, lines


def auto_sleep(sec):
    end = time.time() + sec
    while time.time() < end:
        if AUTO["stop"]:
            return True
        time.sleep(0.1)
    return AUTO["stop"]


def auto_step(pg, st):
    op = st["op"]
    if op in ("move", "click", "double_click", "right_click", "scroll") and "x" in st:
        pg.moveTo(st["x"], st["y"], duration=0.35)
    if op == "click":
        pg.click()
    elif op == "double_click":
        pg.doubleClick()
    elif op == "right_click":
        pg.rightClick()
    elif op == "drag":
        pg.moveTo(st["x"], st["y"], duration=0.35)
        pg.dragTo(st["x2"], st["y2"], duration=0.7, button="left")
    elif op == "scroll":
        pg.scroll(st["amount"])
    elif op == "type":
        pg.write(st["text"], interval=0.03)
    elif op == "press":
        pg.press(st["key"])
    elif op == "hotkey":
        pg.hotkey(*st["keys"])
    elif op == "wait":
        auto_sleep(st["seconds"])


def auto_run(plan, lines, delay):
    try:
        pg = _pg()
        pg.FAILSAFE, pg.PAUSE = True, 0.08  # pushing the mouse into the top-left corner stops everything
        for sec in range(delay, 0, -1):
            AUTO["msg"] = "Starting in %d. Switch to the window you want to control. To stop, click Stop or push the mouse into the top-left corner." % sec
            if auto_sleep(1):
                raise StopIteration
        AUTO["state"] = "running"
        for _ in range(plan["repeat"]):
            for st, d in zip(plan["steps"], lines):
                if AUTO["stop"]:
                    raise StopIteration
                AUTO["i"] += 1
                AUTO["msg"] = "Step %d of %d: %s" % (AUTO["i"], AUTO["n"], d)
                auto_step(pg, st)
        AUTO.update(state="done", msg="Finished all %d steps." % AUTO["n"])
    except StopIteration:
        AUTO.update(state="stopped", msg="Stopped.")
    except Exception as e:
        if e.__class__.__name__ == "FailSafeException":
            AUTO.update(state="stopped", msg="Stopped: the mouse reached the top-left corner.")
        else:
            AUTO.update(state="error", msg="Something went wrong: %s" % e)


def auto_shot():
    import io
    pg = _pg()
    im = pg.screenshot().convert("RGB")
    rw, rh = pg.size()
    im.thumbnail((1280, 1280))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=80)
    return {"b64": base64.b64encode(b.getvalue()).decode(), "w": im.size[0], "h": im.size[1], "rw": rw, "rh": rh}


def auto_api(j):
    a = j.get("action")
    if a == "stop":
        AUTO["stop"] = True
        return {"ok": True}
    if a == "status":
        return dict(AUTO)
    pg = _pg()
    w, h = pg.size()
    if a == "info":
        return {"w": w, "h": h}
    if a == "pos":
        x, y = pg.position()
        return {"x": x, "y": y}
    if a == "shot":
        return auto_shot()
    plan, lines = auto_clean(j.get("plan"), (w, h), set(pg.KEYBOARD_KEYS))
    if a == "validate":
        return {"plan": plan, "lines": lines}
    if a == "run":
        if AUTO["state"] in ("countdown", "running"):
            raise ValueError("Something is already running. Stop it first.")
        AUTO.update(state="countdown", msg="", i=0, n=len(plan["steps"]) * plan["repeat"], stop=False)
        threading.Thread(target=auto_run, args=(plan, lines, max(3, min(num_or(j.get("delay"), 8), 30))), daemon=True).start()
        return dict(AUTO)
    raise ValueError("Unknown action.")


def num_or(v, d):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


# ---------- code helper: real files in a project folder ----------
CODE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode", "target", ".next", ".cache"}
BINEXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".wav", ".zip", ".exe", ".dll",
          ".so", ".pdf", ".pyc", ".ttf", ".woff", ".woff2", ".bin", ".7z", ".rar", ".gz", ".tar", ".dmg", ".pkg", ".db", ".sqlite"}
CODE_SECRET = re.compile(r"(^\.env|\.pem$|\.key$|\.pfx$|^id_rsa|^id_ed25519|^credentials)", re.I)
CODEUNDO = os.path.join(os.path.expanduser("~"), ".my-assistant", "code-undo")


def code_path(f, rel):
    p = (f / rel).resolve()
    if p == f or f not in p.parents:
        raise ValueError("'%s' is outside the project folder, so I won't touch it." % rel)
    if ".git" in p.relative_to(f).parts or CODE_SECRET.search(p.name):
        raise ValueError("I won't touch '%s' (it is private or part of git)." % rel)
    return p


def code_list(folder):
    f, out = safe_folder(folder), []
    for root, dirs, names in os.walk(f):
        dirs[:] = sorted(d for d in dirs if d not in CODE_SKIP and not d.startswith("."))
        if len(Path(root).relative_to(f).parts) >= 5:
            dirs[:] = []
        for n in sorted(names):
            p = Path(root) / n
            if p.suffix.lower() in BINEXT or CODE_SECRET.search(n):
                continue
            try:
                kb = p.stat().st_size // 1024
            except OSError:
                continue
            out.append({"path": p.relative_to(f).as_posix(), "kb": kb})
            if len(out) >= 400:
                return {"root": str(f), "files": out, "more": True}
    return {"root": str(f), "files": out, "more": False}


def code_read(folder, paths):
    f, out = safe_folder(folder), {}
    for rel in list(paths)[:15]:
        p = code_path(f, rel)
        if not p.is_file() or p.suffix.lower() in BINEXT:
            continue
        with open(p, "rb") as fh:
            raw = fh.read(120001)
        if b"\x00" in raw[:2048]:
            continue
        try:
            text = raw[:120000].decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw[:119990].decode("utf-8")  # a multi-byte character may have been cut
            except UnicodeDecodeError:
                continue
        out[rel] = {"text": text, "cut": len(raw) > 120000}
    return {"files": out}


def code_apply(folder, changes):
    f = safe_folder(folder)
    if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
        raise ValueError("Pick between 1 and 20 files to apply.")
    plan = []
    for ch in changes:
        p = code_path(f, str(ch.get("path", "")))
        text = str(ch.get("content", ""))
        if len(text) > 400000:
            raise ValueError("'%s' is too big to write." % ch.get("path"))
        crlf, existed = False, p.exists()
        if existed:
            if not p.is_file():
                raise ValueError("'%s' is a folder." % ch.get("path"))
            if ch.get("expect_new"):
                raise ValueError("'%s' already exists, and the AI never saw it. Tick it and ask again." % ch.get("path"))
            with open(p, "rb") as fh:
                crlf = b"\r\n" in fh.read(8192)
        plan.append((p, text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text, existed))
    shutil.rmtree(CODEUNDO, ignore_errors=True)  # only now: the old undo is replaced by this one
    os.makedirs(CODEUNDO)
    manifest = []
    for i, (p, text, existed) in enumerate(plan):
        bk = None
        if existed:
            bk = os.path.join(CODEUNDO, "%d.bak" % i)
            shutil.copy2(p, bk)
        manifest.append({"path": str(p), "backup": bk})
    with open(os.path.join(CODEUNDO, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    for p, text, _ in plan:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return {"written": len(plan)}


def code_undo():
    try:
        with open(os.path.join(CODEUNDO, "manifest.json")) as fh:
            man = json.load(fh)
    except Exception:
        return {"restored": 0}
    n = 0
    for m in man:
        p = Path(m["path"])
        if m["backup"]:
            shutil.copy2(m["backup"], p)
            n += 1
        elif p.exists():
            p.unlink()
            n += 1
    shutil.rmtree(CODEUNDO, ignore_errors=True)
    return {"restored": n}


def code_api(j):
    a = j.get("action")
    if a == "list":
        return code_list(j["folder"])
    if a == "read":
        return code_read(j["folder"], j.get("paths", []))
    if a == "apply":
        return code_apply(j["folder"], j.get("changes"))
    if a == "undo":
        return code_undo()
    raise ValueError("Unknown action.")


# ---------- Ollama updates ----------
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
UPD = {"state": "idle", "pct": 0, "msg": ""}
LATEST = {"t": 0, "v": ""}


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:3]) or (0,)


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=3) as r:
            return json.loads(r.read()).get("version", "") or "?"
    except Exception:
        return ""


def ollama_info():
    cur = ollama_up()
    if time.time() - LATEST["t"] > 1800 or not LATEST["v"]:  # ask GitHub at most every 30 minutes
        try:
            req = urllib.request.Request("https://api.github.com/repos/ollama/ollama/releases/latest",
                                         headers={"User-Agent": "atlas", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                LATEST.update(t=time.time(), v=json.loads(r.read()).get("tag_name", "").lstrip("v"))
        except Exception:
            pass
    late = LATEST["v"]
    return {"current": cur if cur != "?" else "", "latest": late, "newer": bool(cur and late and vkey(late) > vkey(cur)),
            "os": "windows" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "linux"), "update": dict(UPD)}


def start_ollama():
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama")
    for exe, args in (("ollama app.exe", []), ("ollama.exe", ["serve"])):
        p = os.path.join(base, exe)
        if os.path.exists(p):
            subprocess.Popen([p] + args, creationflags=0x08000008)  # no window, detached
            return


def win_update(latest):
    path = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
    try:
        UPD.update(state="downloading", pct=0, msg="Downloading Ollama %s" % latest)
        req = urllib.request.Request(OLLAMA_SETUP_URL, headers={"User-Agent": "atlas"})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as fh:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
                got += len(b)
                if total:
                    UPD["pct"] = int(got * 100 / total)
        with open(path, "rb") as fh:
            head = fh.read(2)
        if os.path.getsize(path) < 20000000 or head != b"MZ":
            raise ValueError("The download didn't look like the Ollama installer. Use Download latest Ollama instead.")
        UPD.update(state="installing", pct=100, msg="Installing Ollama %s" % latest)
        subprocess.run([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=1800)
        for i in range(30):  # wait for Ollama to answer again, and start it if the installer didn't
            if ollama_up():
                break
            if i == 10:
                start_ollama()
            time.sleep(1)
        ver = ollama_up()
        UPD.update(state="done", msg=("Ollama is now version %s." % ver) if ver else "Installed, but Ollama isn't answering yet. Start it from the Start menu.")
    except Exception as e:
        UPD.update(state="error", msg=str(e) or "The update didn't work. Use Download latest Ollama instead.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------- custom app icon ----------
def save_icon(sizes):
    """The page has already cropped and resized the picture. This stores the PNGs and builds .ico and .icns files."""
    pngs = {}
    for k, v in sizes.items():
        n, b = int(k), base64.b64decode(v)
        if n not in (16, 24, 32, 48, 64, 128, 256, 512) or b[:8] != b"\x89PNG\r\n\x1a\n" or len(b) > 3000000:
            raise ValueError("That icon data wasn't valid. Try another image.")
        pngs[n] = b
    if 256 not in pngs:
        raise ValueError("That icon data wasn't valid. Try another image.")
    shutil.rmtree(ICONDIR, ignore_errors=True)
    os.makedirs(ICONDIR)
    for n, b in pngs.items():
        with open(os.path.join(ICONDIR, "icon-%d.png" % n), "wb") as fh:
            fh.write(b)
    ents = sorted((n, b) for n, b in pngs.items() if n <= 256)
    head, dirs, blob, off = struct.pack("<HHH", 0, 1, len(ents)), b"", b"", 6 + 16 * len(ents)
    for n, b in ents:
        w = 0 if n >= 256 else n
        dirs += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(b), off + len(blob))
        blob += b
    with open(os.path.join(ICONDIR, "atlas.ico"), "wb") as fh:
        fh.write(head + dirs + blob)
    kinds = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09"}
    body = b"".join(kinds[n] + struct.pack(">I", 8 + len(b)) + b for n, b in sorted(pngs.items()) if n in kinds)
    with open(os.path.join(ICONDIR, "atlas.icns"), "wb") as fh:
        fh.write(b"icns" + struct.pack(">I", 8 + len(body)) + body)
    return {"v": int(os.path.getmtime(os.path.join(ICONDIR, "icon-256.png"))), "custom": True}


def page_html():
    html = PAGE.replace("__TOKEN__", TOKEN)
    p = os.path.join(ICONDIR, "icon-256.png")
    if os.path.exists(p):
        link = '<link rel="icon" type="image/png" href="/icon.png?v=%d">' % int(os.path.getmtime(p))
        html = re.sub(r"<!--icon-->.*?<!--/icon-->", lambda m: link, html, flags=re.S)
    return html


# ---------- QR code (versions 1-6, error correction L) ----------
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 256:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
_QV = {1: (19, 7, [(1, 19)]), 2: (34, 10, [(1, 34)]), 3: (55, 15, [(1, 55)]),
       4: (80, 20, [(1, 80)]), 5: (108, 26, [(1, 108)]), 6: (136, 18, [(2, 68)])}
_QA = {2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_QM = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
       lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
       lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
       lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _gm(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs(data, n):
    gen = [1]
    for i in range(n):
        nx = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nx[j] ^= c
            nx[j + 1] ^= _gm(c, _EXP[i])
        gen = nx
    res = list(data) + [0] * n
    for i in range(len(data)):
        c = res[i]
        if c:
            for j in range(1, len(gen)):
                res[i + j] ^= _gm(gen[j], c)
    return res[len(data):]


def _penalty(M):
    n, p = len(M), 0
    for g in (M, list(zip(*M))):
        for row in g:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    p += run - 2 if run >= 5 else 0
                    run = 1
            p += run - 2 if run >= 5 else 0
            t = "".join(map(str, row))
            p += 40 * (t.count("10111010000") + t.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if M[r][c] == M[r][c + 1] == M[r + 1][c] == M[r + 1][c + 1]:
                p += 3
    return p + 10 * (abs(sum(map(sum, M)) * 20 - n * n * 10) // (n * n))


def qr_rows(text):
    """Returns the QR code for text as a list of strings of 0 and 1 (1 = dark)."""
    data = text.encode()
    for ver in range(1, 7):
        if len(data) + 2 <= _QV[ver][0]:
            break
    else:
        raise ValueError("Text is too long for a QR code.")
    cap, ecn, groups = _QV[ver]
    bits = "0100" + format(len(data), "08b") + "".join(format(b, "08b") for b in data)
    bits += "0" * min(4, cap * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    cw = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    while len(cw) < cap:
        cw.append(0xEC if (len(cw) - len(bits) // 8) % 2 == 0 else 0x11)
    blocks, k = [], 0
    for nb, dl in groups:
        for _ in range(nb):
            blocks.append(cw[k:k + dl])
            k += dl
    ecs = [_rs(b, ecn) for b in blocks]
    out = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    out += [e[i] for i in range(ecn) for e in ecs]
    stream = "".join(format(c, "08b") for c in out) + "0" * (0 if ver == 1 else 7)
    n = 17 + 4 * ver
    M = [[0] * n for _ in range(n)]
    F = [[False] * n for _ in range(n)]

    def put(r, c, v):
        if 0 <= r < n and 0 <= c < n:
            M[r][c], F[r][c] = v, True

    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                put(r0 + dr, c0 + dc, int(inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))))
    for i in range(8, n - 8):
        put(6, i, int(i % 2 == 0))
        put(i, 6, int(i % 2 == 0))
    pos = _QA.get(ver, [])
    for r in pos:
        for c in pos:
            if (r == 6 and c == 6) or (r == 6 and c == pos[-1]) or (r == pos[-1] and c == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, int(max(abs(dr), abs(dc)) != 1))
    for i in range(9):  # reserve the format areas
        F[8][i] = F[i][8] = True
    for i in range(8):
        F[8][n - 1 - i] = F[n - 1 - i][8] = True
    F[n - 8][8] = True
    bi, c, up = 0, n - 1, True
    while c > 0:
        if c == 6:
            c -= 1
        for r in (range(n - 1, -1, -1) if up else range(n)):
            for cc in (c, c - 1):
                if not F[r][cc]:
                    M[r][cc] = int(stream[bi]) if bi < len(stream) else 0
                    bi += 1
        up, c = not up, c - 2
    best = None
    for mask in range(8):
        T = [row[:] for row in M]
        for r in range(n):
            for c in range(n):
                if not F[r][c] and _QM[mask](r, c):
                    T[r][c] ^= 1
        d = 8 | mask
        rem = d << 10
        for i in range(14, 9, -1):
            if rem >> i & 1:
                rem ^= 0x537 << (i - 10)
        fb = ((d << 10) | rem) ^ 0x5412
        bit = lambda i: fb >> i & 1
        for i in range(6):
            T[i][8] = bit(i)
        T[7][8], T[8][8], T[8][7] = bit(6), bit(7), bit(8)
        for i in range(9, 15):
            T[8][14 - i] = bit(i)
        for i in range(8):
            T[8][n - 1 - i] = bit(i)
        for i in range(8, 15):
            T[n - 15 + i][8] = bit(i)
        T[n - 8][8] = 1
        sc = _penalty(T)
        if best is None or sc < best[0]:
            best = (sc, T)
    return ["".join(map(str, row)) for row in best[1]]


# ---------- sharing files with a phone over your Wi-Fi ----------
OUTBOX = os.path.join(BASE, "outbox")
SHARE = {"srv": None, "token": "", "port": 0, "ip": "", "recv": None, "out": {}, "got": [], "last": 0, "seq": 0, "qr": []}
MAX_UP = 8 * 1024 ** 3

PHONE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Share files</title>
<style>
:root{color-scheme:dark}
body{margin:0 auto;padding:20px;max-width:560px;background:#121314;color:#e7e7e4;font:16px/1.5 system-ui,sans-serif}
h1{font:600 22px Georgia,serif}h2{font-size:15px;color:#8d9095;margin:26px 0 8px}
.btn{display:block;width:100%;background:#8aa0ff;color:#0d1230;border:0;border-radius:10px;padding:14px;font:600 16px system-ui}
.row{background:#1a1b1d;border:1px solid #2b2d30;border-radius:10px;padding:10px 12px;margin:6px 0;word-break:break-all}
a{color:#8aa0ff}progress{width:100%}
</style></head><body>
<h1>Share with your computer</h1>
<h2>Send to the computer</h2>
<button class="btn" id="pick">Choose files to send</button><input type="file" id="f" multiple hidden>
<div id="up"></div>
<h2>From the computer</h2>
<div id="dl">Loading</div>
<script>
const $=id=>document.getElementById(id),B=location.pathname.replace(/\/*$/,"/");
$("pick").onclick=()=>$("f").click();
$("f").onchange=async e=>{for(const f of [...e.target.files])await send(f);e.target.value=""};
function send(f){
 return new Promise(res=>{
  const row=document.createElement("div"),t=document.createElement("div"),p=document.createElement("progress"),x=new XMLHttpRequest();
  row.className="row";t.textContent=f.name;p.max=100;p.value=0;row.append(t,p);$("up").prepend(row);
  x.open("POST",B+"up");x.setRequestHeader("X-Name",encodeURIComponent(f.name));
  x.upload.onprogress=e=>{if(e.lengthComputable)p.value=e.loaded*100/e.total};
  x.onload=()=>{p.remove();t.textContent=(x.status===200?"Sent: ":"Failed: ")+f.name;res()};
  x.onerror=()=>{p.remove();t.textContent="Failed: "+f.name;res()};
  x.send(f);
 });
}
async function list(){
 try{
  const j=await(await fetch(B+"list")).json(),box=$("dl");
  box.textContent=j.length?"":"Nothing shared from the computer yet.";
  j.forEach(f=>{const r=document.createElement("div"),a=document.createElement("a");r.className="row";a.href=B+"dl/"+f.id;a.download=f.name;a.textContent=f.name;r.append(a," ("+f.mb+" MB)");box.append(r)});
 }catch(e){}
}
list();setInterval(list,5000);
</script></body></html>"""


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # nothing is sent; this just picks the right network card
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def clean_name(n):
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.basename(n.replace("\\", "/"))).strip(". ")
    return n[:150] or "file"


def save_stream(rfile, length, path):
    left = length
    try:
        with open(path, "wb") as fh:
            while left > 0:
                b = rfile.read(min(1 << 20, left))
                if not b:
                    raise OSError("connection closed")
                fh.write(b)
                left -= len(b)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise


def default_recv():
    dl = HOME / "Downloads"
    return str((dl if dl.is_dir() else HOME) / "From Phone")


def share_state():
    d = {"running": SHARE["srv"] is not None, "recv": str(SHARE["recv"] or default_recv())}
    if d["running"]:
        d.update(url="http://%s:%d/s/%s/" % (SHARE["ip"], SHARE["port"], SHARE["token"]), qr=SHARE["qr"],
                 got=SHARE["got"][-30:],
                 out=[{"id": k, "name": v[1], "mb": round(v[2] / 1048576, 1)} for k, v in SHARE["out"].items()])
    return d


def share_stop():
    srv = SHARE["srv"]
    SHARE["srv"] = None
    if srv:
        srv.shutdown()
        srv.server_close()
    shutil.rmtree(OUTBOX, ignore_errors=True)  # the copies you added are deleted when sharing stops
    SHARE["out"] = {}
    return share_state()


def share_watch(srv):
    while SHARE["srv"] is srv:
        time.sleep(10)
        if SHARE["srv"] is srv and time.time() - SHARE["last"] > 1800:  # 30 minutes with no use
            share_stop()


def share_start(recv):
    if SHARE["srv"]:
        return share_state()
    ip = lan_ip()
    if ip.startswith("127."):
        raise ValueError("Couldn't find your network address. Connect this computer to Wi-Fi and try again.")
    d = dest_dir(recv.strip() or default_recv())
    d.mkdir(exist_ok=True)
    shutil.rmtree(OUTBOX, ignore_errors=True)
    os.makedirs(OUTBOX)
    srv = ThreadingHTTPServer(("0.0.0.0", 0), ShareHandler)
    SHARE.update(token=secrets.token_hex(8), ip=ip, port=srv.server_address[1], recv=d,
                 out={}, got=[], last=time.time(), seq=0)
    SHARE["qr"] = qr_rows("http://%s:%d/s/%s/" % (ip, SHARE["port"], SHARE["token"]))
    SHARE["srv"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=share_watch, args=(srv,), daemon=True).start()
    return share_state()


def share_add(name, rfile, length):
    if not SHARE["srv"]:
        raise ValueError("Start sharing first.")
    SHARE["seq"] += 1
    key, nm = str(SHARE["seq"]), clean_name(name)
    path = os.path.join(OUTBOX, key + "_" + nm)
    save_stream(rfile, length, path)
    SHARE["out"][key] = (path, nm, length)
    SHARE["last"] = time.time()
    return share_state()


def open_folder(p):
    if os.name == "nt":
        os.startfile(p)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])


class ShareHandler(BaseHTTPRequestHandler):
    """The only thing reachable from other devices. Every request needs the secret in the link."""

    def log_message(self, *a):
        pass

    def rest(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "s" and SHARE["srv"] and hmac.compare_digest(parts[1], SHARE["token"]):
            SHARE["last"] = time.time()
            return parts[2:]
        return None

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode() if not isinstance(body, str) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        r = self.rest()
        if r is None:
            return self.send(404, "Not found", "text/plain")
        if not r:
            return self.send(200, PHONE_PAGE, "text/html; charset=utf-8")
        if r == ["list"]:
            return self.send(200, [{"id": k, "name": v[1], "mb": round(v[2] / 1048576, 1)} for k, v in SHARE["out"].items()])
        if len(r) == 2 and r[0] == "dl" and r[1] in SHARE["out"] and os.path.exists(SHARE["out"][r[1]][0]):
            path, nm, size = SHARE["out"][r[1]]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(nm))
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 1 << 20)
            except OSError:
                pass
            return
        self.send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.rest() != ["up"]:
            return self.send(404, "Not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UP:
            return self.send(400, {"error": "Bad size"})
        nm = clean_name(urllib.parse.unquote(self.headers.get("X-Name", "file")))
        stem, ext = os.path.splitext(nm)
        path, i = os.path.join(str(SHARE["recv"]), nm), 1
        while os.path.exists(path):  # never overwrite
            path = os.path.join(str(SHARE["recv"]), "%s (%d)%s" % (stem, i, ext))
            i += 1
        try:
            save_stream(self.rfile, length, path)
        except Exception:
            return
        SHARE["got"].append({"name": os.path.basename(path), "mb": round(length / 1048576, 1)})
        self.send(200, {"ok": True})


def find_browser():
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        c = [os.path.join(r, p) for r in roots if r for p in (r"Google\Chrome\Application\chrome.exe",
             r"Microsoft\Edge\Application\msedge.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe")]
    elif sys.platform == "darwin":
        c = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        c = [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "microsoft-edge", "brave-browser", "chromium", "chromium-browser")]
    return next((x for x in c if x and os.path.exists(x)), None)


def open_window(url):
    """Open the app in its own window (no tabs or address bar) using Chrome, Edge or Brave."""
    exe = None if "--tab" in sys.argv else find_browser()
    if not exe:
        webbrowser.open(url)
        return
    prof = os.path.join(os.path.expanduser("~"), ".my-assistant", "browser-profile")
    proc = subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + prof, "--window-size=1280,860",
                             "--no-first-run", "--no-default-browser-check"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait():
        t0 = time.time()
        proc.wait()
        if time.time() - t0 > 5:  # the window was closed, so quit the app
            os._exit(0)
    threading.Thread(target=wait, daemon=True).start()


def watchdog():
    """Quit when the browser tab has been closed for a while."""
    prev = time.time()
    while True:
        time.sleep(5)
        now = time.time()
        if now - prev > 20:  # the computer was asleep
            PING["t"] = now
        prev = now
        if PING["seen"] and now - PING["t"] > 150:
            os._exit(0)


def make_shortcut():
    frozen = getattr(sys, "frozen", False)
    script = sys.executable if frozen else os.path.abspath(__file__)
    py = sys.executable
    home = Path.home()
    if os.name == "nt":
        if not frozen:
            w = py.replace("python.exe", "pythonw.exe")
            py = w if os.path.exists(w) else py
        target, args = (script, "") if frozen else (py, '"%s"' % script)
        ico = os.path.join(ICONDIR, "atlas.ico")
        if not os.path.exists(ico):
            ico = os.path.join(os.path.dirname(script), "assets", "atlas.ico")
            ico = ico if os.path.exists(ico) and not frozen else target
        ps = ('$d=[Environment]::GetFolderPath("Desktop");'
              '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+"\\Atlas.lnk");'
              '$s.TargetPath="%s";$s.Arguments=\'%s\';$s.WorkingDirectory="%s";$s.IconLocation="%s,0";$s.Save()'
              % (target, args, os.path.dirname(script), ico))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        say("Added 'Atlas' to your Desktop.")
        return
    desk = home / "Desktop"
    desk.mkdir(exist_ok=True)
    cmd = shlex.quote(script) if frozen else shlex.quote(py) + " " + shlex.quote(script)
    if sys.platform == "darwin":
        f = desk / "Atlas.command"
        f.write_text("#!/bin/bash\ncd %s\nexec %s\n" % (shlex.quote(os.path.dirname(script)), cmd))
    else:
        f = desk / "atlas.desktop"
        icon = os.path.join(ICONDIR, "icon-256.png")
        if not os.path.exists(icon):
            icon = os.path.join(os.path.dirname(script), "assets", "atlas-256.png")
            icon = icon if not frozen else ""
        extra = "Icon=%s\n" % icon if icon and os.path.exists(icon) else ""
        f.write_text("[Desktop Entry]\nType=Application\nName=Atlas\nExec=%s\nTerminal=false\n%s" % (cmd, extra))
    f.chmod(0o755)
    say("Added a shortcut to your Desktop:", f)


if __name__ == "__main__":
    if "--shortcut" in sys.argv:
        make_shortcut()
        sys.exit()
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running, so just open it
        open_window(url)
        sys.exit()
    say(f"Atlas is running at {url}\nClose the browser tab (or press Ctrl+C) to stop.")
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Timer(0.8, lambda: open_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")
