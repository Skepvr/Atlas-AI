MY ASSISTANT
A private AI chat app that runs on your own computer using Ollama. Nothing is sent online.

WHAT YOU NEED (one time)
1. Ollama: download it free from ollama.com and install it.
2. A model: open a terminal and run   ollama pull llama3.2
3. Python 3 from python.org (skip this if you were given MyAssistant.exe / MyAssistant.app).

START IT
- Windows: double-click "Start My Assistant.bat"
- Mac / Linux: open a terminal in this folder and run   python3 assistant.py

MAKE A DESKTOP SHORTCUT
Run   python assistant.py --shortcut   once (Mac/Linux: python3). A shortcut appears on your Desktop.

CLOSING IT
Close the browser tab and the app quits by itself a couple of minutes later.

Your chats are saved on your computer and are never shared.

OPENS IN ITS OWN WINDOW
If Chrome, Edge or Brave is installed, the app opens in its own window (no tabs). Closing the window quits the app.
Add the word --tab when starting it to use a normal browser tab instead.

CONVERTING VIDEOS AND IMAGES
Needs ffmpeg. Easiest: run   pip install imageio-ffmpeg   once.

SHARE FILES WITH YOUR PHONE
Click "Share with phone", scan the QR code with your phone camera (same Wi-Fi), and send or download files in the page that opens. It turns off after 30 minutes of no use.

Commands:
python assistant.py (on Mac or Linux, python3 assistant.py)
Build application: python build_app.py