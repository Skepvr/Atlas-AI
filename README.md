# Atlas

Atlas is a private AI assistant that runs on your own computer using [Ollama](https://ollama.com). Your chats stay on your machine. It is one Python file with no installs needed to run it.

## What it can do

- **Chat** with any model you have in Ollama, with streaming replies, topic groups and search.
- **Read aloud, copy, and "make it sound like me"** on every reply, with a choice of voices.
- **Voice mode**: talk to it hands-free (Chrome or Edge).
- **Reference files**: add notes, PDFs or web pages and ask questions about them.
- **Images and video**: attach a picture or a video clip and ask about it (needs a vision model such as `gemma3:4b`).
- **Game maker**: describe a game, play it in a live preview, and ask for changes.
- **Tools menu**: organize a folder, preview and send videos and images, convert files (needs ffmpeg), share files with your phone through a QR code, and upload a folder to GitHub.

## Get started

1. Install [Ollama](https://ollama.com) and download a model, for example `ollama pull llama3.2`.
2. Install [Python 3.9 or newer](https://python.org).
3. Download this repository (green **Code** button, then **Download ZIP**) and unzip it.
4. Run it:
   - Windows: double-click `Start Atlas.bat`
   - Mac or Linux: `python3 assistant.py`

It opens in its own window using Chrome, Edge or Brave (or a normal browser tab if you add `--tab`). Closing the window quits the app.

Optional extras:

- `pip install pypdf` to read PDFs
- `pip install imageio-ffmpeg` (or install ffmpeg) to convert files
- `python assistant.py --shortcut` adds a desktop shortcut
- `python build_app.py` builds a standalone app you can send to people

## Privacy and safety

- Everything runs locally. The main app only listens on your own computer.
- Voice mode uses the browser's speech recognition, which sends your voice to Google or Microsoft. Reading aloud stays on your computer.
- Sharing with your phone opens a temporary link on your Wi-Fi that turns off after 30 minutes unused. Anyone on that network with the link could use it, and it is not encrypted.
- The GitHub uploader skips files that look private and stores your token only on your computer.
- Your chats are saved in `assistant_data.json` next to the script. It is listed in `.gitignore`, so it won't be uploaded by accident.

## Commands to Know

-python assistant.py (on Mac or Linux, python3 assistant.py)
-Build application: python build_app.py
