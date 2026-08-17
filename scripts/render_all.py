#!/usr/bin/env python3
"""Daily Market Brief — all-in-one renderer for GitHub Actions.

The workflow fetches today's `script.txt` and `today.json` from Google Drive, then runs this.
This script: (1) ensures episodes.json + cover.png exist, (2) renders each line of the two-host
script with Google Cloud TTS, (3) stitches to episodes/<date>.mp3, (4) updates episodes.json
(keeping the last 21) and regenerates feed.xml. Everything else (git commit/push, Pages) is the
workflow's job.

Env:
  GCP_TTS_API_KEY   Google Cloud API key (Text-to-Speech API enabled)
  GITHUB_REPOSITORY "owner/repo" (auto-set by Actions) -> Pages base URL
"""
import os, re, io, json, base64, subprocess, sys, time, html
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY  = os.environ["GCP_TTS_API_KEY"]
ENDPOINT = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={KEY}"

repo = os.environ.get("GITHUB_REPOSITORY", "ATAD-byte/market-brief")
owner, name = repo.split("/", 1)
BASE_URL = f"https://{owner}.github.io/{name}"

META = os.path.join(ROOT, "episodes.json")
FEED = os.path.join(ROOT, "feed.xml")
COVER = os.path.join(ROOT, "cover.png")
KEEP = 21

# ---- load today's metadata + script (fetched by the workflow) ----
with open(os.path.join(ROOT, "today.json")) as f:
    meta = json.load(f)
date = meta["date"]

VOICES = meta.get("voices", {
    "JACK":   {"languageCode": "en-AU", "name": "en-AU-Neural2-B"},   # male anchor
    "SOPHIE": {"languageCode": "en-AU", "name": "en-AU-Neural2-A"},   # female analyst
})
RATE = meta.get("rate", 1.0)
DEFAULT_VOICE = VOICES["JACK"]

# ---- ensure episodes.json ----
if os.path.exists(META):
    with open(META) as f:
        store = json.load(f)
    store["base_url"] = BASE_URL
else:
    store = {
        "title": "Tony's Daily Market Brief",
        "author": "Market Desk",
        "email": "a.tadros05@gmail.com",
        "description": "A daily two-host market brief covering Australian, US and global economic and financial news, auto-generated each morning from Tony's newsletter inbox.",
        "base_url": BASE_URL,
        "episodes": [],
    }

# ---- ensure cover.png (generated once) ----
if not os.path.exists(COVER):
    try:
        from PIL import Image, ImageDraw, ImageFont
        import math
        W = 1500; img = Image.new("RGB", (W, W), (15, 17, 21)); d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 14], fill=(91, 141, 239))
        def font(sz, bold=True):
            p = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
            try: return ImageFont.truetype(p, sz)
            except Exception: return ImageFont.load_default()
        pts = [(120 + i*(W-240)//40, 900 - int(180*math.sin(i/4.0)) - i*6) for i in range(41)]
        d.line(pts, fill=(63, 185, 80), width=10, joint="curve")
        d.text((120, 150), "DAILY", font=font(90), fill=(91, 141, 239))
        d.text((120, 250), "MARKET", font=font(150), fill=(255, 255, 255))
        d.text((120, 410), "BRIEF", font=font(150), fill=(255, 255, 255))
        d.text((120, 1180), "Australia · US · Global", font=font(58, False), fill=(154, 163, 178))
        d.text((120, 1270), "Auto-generated every morning", font=font(48, False), fill=(120, 128, 140))
        img.save(COVER)
        print("generated cover.png")
    except Exception as e:
        print("cover generation skipped:", e)

# ---- parse script ----
lines, spk, buf = [], None, []
srx = re.compile(r"^([A-Z]{2,10}):\s*(.*)$")
for raw in open(os.path.join(ROOT, "script.txt"), encoding="utf-8"):
    raw = raw.rstrip("\n"); m = srx.match(raw)
    if m:
        if spk: lines.append((spk, " ".join(buf).strip()))
        spk, buf = m.group(1), [m.group(2)]
    elif raw.strip():
        buf.append(raw.strip())
if spk: lines.append((spk, " ".join(buf).strip()))
print(f"Parsed {len(lines)} utterances for {date}")

WORK = os.path.join(ROOT, "segments"); os.makedirs(WORK, exist_ok=True)

def tts(text, voice, out_path, retries=4):
    body = json.dumps({
        "input": {"text": text[:4900]},
        "voice": voice,
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": RATE, "sampleRateHertz": 44100},
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    for a in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            open(out_path, "wb").write(base64.b64decode(data["audioContent"]))
            return True
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:300]
            print(f"  HTTP {e.code} (try {a+1}): {msg}")
            if e.code in (429, 500, 502, 503): time.sleep(3*(a+1)); continue
            raise
        except Exception as e:
            print(f"  err (try {a+1}): {e}"); time.sleep(3*(a+1))
    return False

segs = []
for i, (s, t) in enumerate(lines):
    if not t: continue
    mp3 = f"{WORK}/seg_{i:03d}.mp3"
    if tts(t, VOICES.get(s, DEFAULT_VOICE), mp3): segs.append(mp3)
    else: print(f"  ! failed seg {i} ({s})")
print(f"Rendered {len(segs)} segments")
if not segs:
    sys.exit("No audio rendered — aborting.")

# ---- stitch ----
norm = []
for i, m in enumerate(segs):
    nw = f"{WORK}/norm_{i:03d}.wav"
    subprocess.run(["ffmpeg","-y","-i",m,"-af","apad=pad_dur=0.35","-ar","44100","-ac","1",nw],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    norm.append(nw)
listf = f"{WORK}/concat.txt"
open(listf,"w").write("".join(f"file '{os.path.abspath(n)}'\n" for n in norm))
stitched = f"{WORK}/stitched.wav"
subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",listf,"-c","copy",stitched],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.makedirs(os.path.join(ROOT, "episodes"), exist_ok=True)
out_mp3 = os.path.join(ROOT, "episodes", f"{date}.mp3")
subprocess.run(["ffmpeg","-y","-i",stitched,"-af","loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a","libmp3lame","-b:a","128k",out_mp3],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
dur = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",out_mp3],
                     capture_output=True, text=True).stdout.strip()
mm = int(float(dur)//60); ss = int(float(dur)%60); duration = f"{mm}:{ss:02d}"
print(f"episode {date}.mp3  {duration}  {os.path.getsize(out_mp3)/1e6:.1f}MB")

# ---- update episodes.json (replace same date, keep newest KEEP, prune old mp3s) ----
store["episodes"] = [e for e in store["episodes"] if e["date"] != date]
store["episodes"].append({
    "date": date, "title": meta["title"], "summary": meta.get("summary",""),
    "file": f"episodes/{date}.mp3", "duration": duration,
    "bytes": os.path.getsize(out_mp3), "pubdate": meta["pubdate"],
})
store["episodes"].sort(key=lambda e: e["date"], reverse=True)
keep, drop = store["episodes"][:KEEP], store["episodes"][KEEP:]
store["episodes"] = keep
referenced = {e["file"] for e in keep}
for e in drop:
    p = os.path.join(ROOT, e["file"])
    if os.path.exists(p) and e["file"] not in referenced:
        os.remove(p); print("pruned", e["file"])
json.dump(store, open(META, "w"), indent=2)

# ---- regenerate feed.xml ----
def esc(s): return html.escape(s or "", quote=True)
import datetime
now = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
items = []
for ep in sorted(store["episodes"], key=lambda e: e["date"], reverse=True):
    url = f"{BASE_URL}/{ep['file']}"
    items.append(f"""    <item>
      <title>{esc(ep['title'])}</title>
      <description>{esc(ep.get('summary',''))}</description>
      <pubDate>{esc(ep['pubdate'])}</pubDate>
      <enclosure url="{esc(url)}" length="{ep.get('bytes',0)}" type="audio/mpeg"/>
      <guid isPermaLink="false">{esc(url)}</guid>
      <itunes:duration>{esc(ep.get('duration',''))}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{esc(store['title'])}</title>
    <link>{esc(BASE_URL)}/</link>
    <language>en-au</language>
    <description>{esc(store['description'])}</description>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{esc(store['author'])}</itunes:author>
    <itunes:summary>{esc(store['description'])}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
    <itunes:owner><itunes:name>{esc(store['author'])}</itunes:name><itunes:email>{esc(store.get('email',''))}</itunes:email></itunes:owner>
    <itunes:image href="{esc(BASE_URL)}/cover.png"/>
    <itunes:category text="Business"><itunes:category text="Investing"/></itunes:category>
    <image><url>{esc(BASE_URL)}/cover.png</url><title>{esc(store['title'])}</title><link>{esc(BASE_URL)}/</link></image>
{chr(10).join(items)}
  </channel>
</rss>
"""
open(FEED, "w", encoding="utf-8").write(xml)
print(f"feed.xml updated with {len(store['episodes'])} episode(s); base {BASE_URL}")
