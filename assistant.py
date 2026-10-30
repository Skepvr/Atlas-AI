#!/usr/bin/env python3
"""
Atlas: a private chat app for your local Ollama models.

Run:   python assistant.py      (Mac/Linux: python3 assistant.py)
Then:  it opens http://localhost:8765 in your browser.

Needs only Python 3 (no installs). Ollama must be running with at least one model.
Your chats are saved next to this file in assistant_data.json and never leave your computer.
"""
import hmac, json, mimetypes, os, re, secrets, shlex, shutil, socket, subprocess, sys, time, threading, webbrowser, urllib.request, urllib.error, urllib.parse
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
    return {"videos": [v for _, v in sorted(items, key=lambda t: -t[0])][:400]}


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
        done.append({"src": str(a), "dst": str(n), "mode": mode})
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
<title>Atlas</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%238aa0ff'/><circle cx='32' cy='32' r='12' fill='%230d1230'/></svg>">
<style>
:root{color-scheme:dark;--bg:#121314;--panel:#1a1b1d;--ink:#e7e7e4;--mute:#8d9095;--line:#2b2d30;--accent:#8aa0ff;--onaccent:#0d1230;--me:#242a4a;--code:#0b0c0d}
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
#status{max-width:760px;width:100%;margin:0 auto;padding:0 16px;color:#ff9b93;font-size:14px;min-height:0}
@media (prefers-color-scheme:dark){#status{color:#ff9b93}}
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
#toolmenu{position:absolute;top:44px;left:0;right:0;z-index:30;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px;box-shadow:0 12px 32px rgba(0,0,0,.55)}
#toolmenu[hidden]{display:none}
#toolmenu button{display:block;width:100%;text-align:left;background:none;border:0;border-radius:8px;padding:7px 10px}
#toolmenu button:hover,#toolmenu button:focus-visible{background:var(--bg)}
#toolmenu b{display:block;font-weight:600;font-size:14px}
#toolmenu span{display:block;font-size:12px;color:var(--mute)}
.mgroup{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);padding:8px 10px 2px}
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
#status.ok{color:var(--mute)}
.doc{display:flex;gap:6px;align-items:center;font-size:13px;margin-bottom:4px}
.doc span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dialog{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:12px;max-width:560px;width:92%}
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
   <div class="mgroup">Create</div>
   <button role="menuitem" id="makegame"><b>Make a game</b><span>Describe it, play it, edit it live</span></button>
   <div class="mgroup">Files</div>
   <button role="menuitem" id="vids"><b>Videos and images</b><span>Preview, pick and send to a folder</span></button>
   <button role="menuitem" id="convbtn"><b>Convert files</b><span>Change video and image formats</span></button>
   <button role="menuitem" id="tidy"><b>Organize a folder</b><span>Sort Downloads by type, with undo</span></button>
   <button role="menuitem" id="docsbtn"><b>Reference files</b><span>Guides and notes the AI can read</span></button>
   <div class="mgroup">Share</div>
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
 <div id="vgrid"></div>
 <button class="btn" id="vgo">Send ticked videos</button><button class="btn ghost" id="vundo">Undo last run</button><button class="btn ghost" id="vclose">Close</button>
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
 <div class="smsg" role="status"></div>
 <button class="btn" id="setclose">Done</button>
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
const fixErr=m=>/unknown model architecture|mllama/i.test(m||"")?"The model you picked needs a newer version of Ollama than the one installed. Update Ollama (it is free and keeps your downloaded models): get the latest from ollama.com. Or choose a different model that can see images, for example run  ollama pull gemma3:4b  in a terminal.":m;
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
 const r=await fetch("/api/chat",{method:"POST",headers:H,signal:ctl.signal,body:JSON.stringify({model:S.model,stream:true,keep_alive:"30m",options:Object.assign({num_ctx:8192},opts),messages})});
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
 VF.forEach(v=>{
  const c=document.createElement("div");c.className="vc";
  const isImg=/\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif)$/i.test(v.name),vid=document.createElement(isImg?"img":"video");
  if(isImg){vid.alt=v.name;vid.style.cssText="width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px"}else{vid.controls=true;vid.muted=true;vid.preload="metadata"}
  vid.dataset.src=vsrc(v)+(isImg?"":"#t=1");
  const lab=document.createElement("label"),cb=document.createElement("input"),t=document.createElement("span");
  lab.style.cssText="display:flex;gap:6px;align-items:flex-start;margin:4px 0;color:var(--ink)";
  cb.type="checkbox";cb.checked=!!v.sel;cb.style.width="auto";cb.onchange=()=>{v.sel=cb.checked;vcount()};
  t.textContent=v.name+" ("+v.mb+" MB)";lab.append(cb,t);
  const ai=document.createElement("button");ai.className="btn ghost";ai.style.cssText="padding:3px 8px;font-size:12px";ai.textContent="Ask AI about this";
  ai.onclick=async()=>{
   try{if(isImg){PA.imgs.push(await shrink(await(await fetch(vsrc(v))).blob()))}else{const n=await watch(vsrc(v),v.name,vsay);PA.notes.push(n);PA.labels.push(v.name)}renderPA();$("vdlg").close();$("input").focus();status("Video notes attached. Ask your question.",true)}
   catch(e){vsay(e.message)}
  };
  c.append(vid,lab,ai);g.append(c);io.observe(vid);
 });
}
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

async function send(text){
 if(busy){ctl&&ctl.abort();return}
 text=(text||"").trim();
 if(!text&&!(PA.imgs.length||PA.notes.length))return;
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
function openSettings(){loadModels();$("setdlg").showModal()}
$("toolsbtn").onclick=e=>{e.stopPropagation();toolMenu($("toolmenu").hidden)};
$("toolmenu").addEventListener("click",e=>{if(e.target.closest("button"))toolMenu(false)});
document.addEventListener("click",e=>{if(!e.target.closest("#toolmenu,#toolsbtn"))toolMenu(false)});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!$("toolmenu").hidden){toolMenu(false);$("toolsbtn").focus()}});
$("gearbtn").onclick=openSettings;$("modelchip").onclick=openSettings;
$("setclose").onclick=()=>$("setdlg").close();
$("voicesel").onchange=e=>{S.voice=e.target.value;save()};
$("vrate").oninput=e=>{S.rate=+e.target.value;vlabels();save()};
$("vpitch").oninput=e=>{S.pitch=+e.target.value;vlabels();save()};
$("vprev").onclick=()=>speak("Hi! This is how I sound. Pick the voice you like best.");
$("docsbtn").onclick=()=>$("docdlg").showModal();$("docclose").onclick=()=>$("docdlg").close();
$("vids").onclick=openVideos;$("gh").onclick=openGH;
$("vclose").onclick=()=>$("vdlg").close();$("ghclose").onclick=()=>$("ghdlg").close();
$("vload").onclick=async()=>{
 try{vsay("Loading\u2026");const j=await api("/api/videos",{folder:$("vfolder").value,deep:$("vdeep").checked});VF=j.videos;vRender();
  vsay(VF.length?VF.length+" videos. Tick the ones you want.":"No video files found there. Try the folder that holds them, or tick the subfolders box.")}
 catch(e){vsay(e.message)}
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
$("vundo").onclick=async()=>{try{const j=await api("/api/undo");vsay("Undid "+j.n+" videos.");$("vload").click()}catch(e){vsay(e.message)}};
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
 try{const r=await fetch("/api/state");Object.assign(S,await r.json())}catch(e){}
 $("system").value=S.system;$("preset").value=S.preset;$("voicesample").value=S.voiceSample||"";fetch("/api/ping").catch(()=>{});setInterval(()=>fetch("/api/ping").catch(()=>{}),15000);$("mode").value=S.mode;$("game").value=S.game;$("notes").value=S.notes;$("gamebox").hidden=S.mode!=="game";
 ["vfolder","vdest","vmode","vdeep","cfolder","cdeep","cfmt","cdest","ghfolder","ghrepo"].forEach(k=>remember(k,k));
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
        if path == "/api/convert":
            return convert_file(j["path"], j["fmt"], j.get("dest", ""))
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
        if self.path.startswith("/api/file?"):
            return self.serve_file()
        if self.path == "/":
            self.reply(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
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
        elif self.path in ("/api/extract", "/api/url", "/api/plan", "/api/run", "/api/undo", "/api/warm", "/api/docs", "/api/videos", "/api/send", "/api/convert", "/api/share", "/api/shareopen", "/api/ghtoken", "/api/ghcheck", "/api/openurl", "/api/ghplan", "/api/ghgo"):
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
        ps = ('$d=[Environment]::GetFolderPath("Desktop");'
              '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+"\\Atlas.lnk");'
              '$s.TargetPath="%s";$s.Arguments=\'%s\';$s.WorkingDirectory="%s";$s.Save()'
              % (target, args, os.path.dirname(script)))
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
        f.write_text("[Desktop Entry]\nType=Application\nName=Atlas\nExec=%s\nTerminal=false\n" % cmd)
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
