import os
import re
import json
import uuid
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, redirect, url_for, session
import ollama
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "video-ai-minimal-key")

MODEL = "llama3.2"
DATA_FILE = os.path.join(os.path.dirname(__file__), "chat_history.json")

# ----------------- STORAGE HELPERS -----------------
def load_chats():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_chats():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(chats, f, ensure_ascii=False, indent=2)

chats = load_chats()

def create_chat():
    chat_id = str(uuid.uuid4())
    chats[chat_id] = {
        "title": "New Chat",
        "messages": [],
        "video": None,
        "suggestions": []
    }
    save_chats()
    return chat_id

# ----------------- YOUTUBE HELPERS -----------------
def get_video_id(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")

    if host == "youtu.be":
        return parsed.path.strip("/")

    if host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com"):
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            return qs.get("v", [None])[0]
        m = re.match(r"/(shorts|embed|live)/([^/?]+)", parsed.path)
        if m:
            return m.group(2)
    return None

def get_video_info(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # Best quality thumbnail picker
    thumb = info.get("thumbnail") or ""
    if info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url", thumb)

    video = {
        "title": info.get("title", "Unknown Title"),
        "url": info.get("webpage_url", url),
        "thumbnail": thumb,
        "channel": info.get("channel") or info.get("uploader", "Unknown Channel"),
        "duration": info.get("duration_string") or "",
        "duration_sec": info.get("duration", 0),
        "description": info.get("description", "")[:2500]
    }

    vid = get_video_id(url)
    transcript_text = ""

    if vid:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(vid)
            try:
                t_obj = transcript_list.find_transcript(['en', 'hi', 'en-US', 'en-GB', 'hi-Latn'])
            except Exception:
                t_obj = transcript_list.find_generated_transcript(['en', 'hi'])
            
            tr = t_obj.fetch()
            lines = []
            for t in tr:
                sec = int(t['start'])
                mm = sec // 60
                ss = sec % 60
                lines.append(f"[{mm:02d}:{ss:02d}] {t['text']}")
            transcript_text = "\n".join(lines)
        except Exception:
            transcript_text = ""

    video["transcript"] = transcript_text[:14000]
    return video

def search_related_videos(query):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"ytsearch4:{query}", download=False)

        res = []
        for item in data.get("entries", []):
            if not item:
                continue
            vid_id = item.get("id")
            res.append({
                "title": item.get("title"),
                "channel": item.get("channel") or item.get("uploader", ""),
                "thumbnail": item.get("thumbnail", ""),
                "url": item.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
            })
        return res
    except Exception:
        return []

# ----------------- AI CONTEXT ENGINE -----------------
def ask_with_context(chat, question):
    messages = [{
        "role": "system",
        "content": (
            "You are VideoAI Assistant. Answer in a clear, friendly, and structured manner. "
            "Use bullet points for points and bold text where relevant. "
            "If referring to moments in the video, include the timestamp [MM:SS]."
        )
    }]

    if chat.get("video"):
        v = chat["video"]
        ctx = f"CURRENT LOADED VIDEO:\nTitle: {v['title']}\nChannel: {v['channel']}\nDuration: {v['duration']}\n"
        if v.get("transcript"):
            ctx += f"\nTIMED TRANSCRIPT:\n{v['transcript']}"
        elif v.get("description"):
            ctx += f"\nDESCRIPTION:\n{v['description']}"
        messages.append({"role": "system", "content": ctx})

    for m in chat["messages"][-6:]:
        if m["role"] in ["user", "assistant"]:
            messages.append({"role": m["role"], "content": m["text"]})

    messages.append({"role": "user", "content": question})

    try:
        resp = ollama.chat(
            model=MODEL, 
            messages=messages, 
            options={"temperature": 0.3, "num_predict": 600}
        )
        return resp["message"]["content"]
    except Exception as e:
        return f"Ollama connection error. Ensure Ollama is running ({str(e)})."

# ----------------- FLASK ROUTES -----------------
@app.route("/", methods=["GET", "POST"])
def index():
    if "chat_id" not in session or session["chat_id"] not in chats:
        session["chat_id"] = create_chat()

    chat_id = session["chat_id"]
    chat = chats[chat_id]

    if request.method == "POST":
        user_msg = request.form.get("message", "").strip()
        if not user_msg:
            return redirect(url_for("index"))

        chat["messages"].append({
            "role": "user", 
            "text": user_msg, 
            "time": datetime.now().strftime("%H:%M")
        })

        if chat["title"] == "New Chat":
            chat["title"] = user_msg[:28] + ("..." if len(user_msg) > 28 else "")

        url_match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)\S+", user_msg)

        if url_match:
            try:
                link = url_match.group(0).rstrip(".,)")
                vid = get_video_info(link)
                chat["video"] = vid
                chat["suggestions"] = []

                prompt = f"""
You are an expert Video AI Analyst.
Analyze the video below and format your output clearly with Markdown headers, neat bullet points, and timestamps.

VIDEO DETAILS:
- Title: {vid['title']}
- Channel: {vid['channel']}
- Duration: {vid['duration']}
- Description: {vid['description']}

TRANSCRIPT DATA:
{vid['transcript'] if vid['transcript'] else "No subtitles available (Build timeline & chunks based on video description, title, and standard flow)."}

RESPOND STRICTLY IN THESE 4 STRUCTURED SECTIONS:

### 📌 1. Executive Summary
Give an engaging 3-4 sentence comprehensive summary of what this video is about.

### 🔑 2. Key Highlights & Takeaways
- Key Point 1
- Key Point 2
- Key Point 3
- Key Point 4

### ⏱️ 3. Timeline Breakdown & Timestamps
Provide chronological chunks in [MM:SS] format:
- [00:00] Introduction / Theme Setup
- [01:00] Core Concept / First Phase
- [02:30] Key Deep-Dive / Climax
- ...

### 💡 4. Core Takeaway
A 1-2 sentence concluding note about the value of this video.
"""
                ans = ollama.chat(
                    model=MODEL, 
                    messages=[{"role": "user", "content": prompt}], 
                    options={"temperature": 0.3, "num_predict": 850}
                )
                bot_ans = ans["message"]["content"]
                chat["messages"].append({
                    "role": "assistant", 
                    "text": bot_ans, 
                    "time": datetime.now().strftime("%H:%M")
                })
            except Exception as e:
                chat["messages"].append({
                    "role": "assistant", 
                    "text": f"Error analyzing video: {str(e)}", 
                    "time": datetime.now().strftime("%H:%M")
                })
        else:
            q = user_msg.lower()
            related_keywords = [
                "related video", "related videos", "similar video", 
                "similar videos", "videos like this", "suggest video", 
                "recommend video", "suggest more videos"
            ]

            if chat.get("video") and any(k in q for k in related_keywords):
                title = chat["video"]["title"]
                suggestions = search_related_videos(title)
                chat["suggestions"] = suggestions
                if suggestions:
                    bot_ans = "Here are related video recommendations. Click any card below to watch directly on YouTube:"
                else:
                    bot_ans = "Could not fetch related videos right now."
                chat["messages"].append({
                    "role": "assistant", 
                    "text": bot_ans, 
                    "time": datetime.now().strftime("%H:%M")
                })
            else:
                bot_ans = ask_with_context(chat, user_msg)
                chat["messages"].append({
                    "role": "assistant", 
                    "text": bot_ans, 
                    "time": datetime.now().strftime("%H:%M")
                })

        save_chats()
        return redirect(url_for("index"))

    return render_template("index.html", chats=chats, current_chat=chat, chat_id=chat_id)

@app.route("/new")
def new_chat():
    cid = create_chat()
    session["chat_id"] = cid
    return redirect(url_for("index"))

@app.route("/open/<chat_id>")
def open_chat(chat_id):
    if chat_id in chats:
        session["chat_id"] = chat_id
    return redirect(url_for("index"))

@app.route("/delete/<chat_id>", methods=["POST"])
def delete_chat(chat_id):
    if chat_id in chats:
        chats.pop(chat_id)
        save_chats()
    if session.get("chat_id") == chat_id:
        session.pop("chat_id", None)
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)