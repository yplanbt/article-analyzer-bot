"""YouTube Title Generator — bodycam niche specialist using Gemini + Claude."""

import json
import re
import anthropic
from youtube_transcript_api import YouTubeTranscriptApi

# ═══════════════════════════════════════════════════════════════════════════════
# BODYCAM NICHE RESEARCH DATABASE (from 191 videos across 10 top channels)
# ═══════════════════════════════════════════════════════════════════════════════

TITLE_PATTERNS = {
    "structures": {
        "How X Turns into Y": {
            "frequency": "30% of top 50",
            "examples": [
                "How Officers Find Out He's Got a Body in the House",
                "How a Driveway Confrontation Turns a Citation into an Arrest",
                "How a Restaurant Complaint Becomes an Arrestable Offense",
                "How Demanding a Store Discount Turns into an Arrest",
                "How a 21-Year-Old Turns a Misdemeanor into a Felony",
                "How Some Flashing Blue Lights Turns into a Police Impersonation Charge",
                "How to Immediately Get Pulled Over by the Police",
            ],
        },
        "The Moment X": {
            "frequency": "8% of top 50",
            "examples": [
                "The Moment She Realized She Killed 2 People",
                "The Moment He Realized He Killed Someone",
                "The Moment She Discovered Her Husband's Horrifying Secret",
                "The Fake UPS Delivery That Turned Deadly",
                "The Case of the Minnesota Assassin",
            ],
        },
        "When X": {
            "frequency": "12% of top 50",
            "examples": [
                "When a Walmart Shoplifter Thinks She's Entitled to Steal",
                "When the Suspects Try to Take Over the Investigation",
            ],
        },
        "[Adjective] [Person] [Action]": {
            "frequency": "25% of top 50",
            "examples": [
                "Racist Woman Arrested After Attacking Senior Citizen at the Airport",
                "Entitled 18-Year-Old Causes Complete Chaos During Arrest",
                "Disturbed Woman Kicked Off Airplane for Outrageous Behavior",
                "Teenage Illegal Immigrant Caught Trafficking $700,000 Worth Of Illegal Substance",
                "High School Student Arrested After Flashing Handgun At Students In Class Room",
                "Abusive Woman Punches Publix Employee In Front Of Police",
            ],
        },
    },
    "power_phrases": [
        "The Moment", "Realized", "Turns into", "Won't Forget",
        "Doesn't End Well", "Caught", "Lesson", "Entitled",
        "Complete Chaos", "Goes Off", "Thinks She's/He's",
        "Here's Why You Don't", "Led Police to", "Real-Life",
    ],
    "character_archetypes": [
        "Woman", "Man", "Teen", "[Age]-Year-Old", "Mom/Mother",
        "Officer/Cop", "Student", "Employee", "Suspect", "Father/Dad",
    ],
    "emotional_triggers": [
        "Arrest", "Kill/Dead/Death", "Realize/Realized",
        "Turns into", "Chaos", "Lesson", "Gun/Gunfight",
        "Caught", "Steal/Stole/Stolen", "Rescue", "Entitled",
        "Confrontation", "Deadly", "Secret", "Discover",
    ],
    "avg_title_length": 56,
    "optimal_range": (35, 75),
}

# Long-form only (8+ min) — 417 videos researched across 10 channels, zero shorts
TOP_PERFORMING_TITLES = [
    {"title": "The Moment She Realized She Killed 2 People", "views": 20409844, "ratio": 26.54},
    {"title": "Racist Woman Arrested After Attacking Senior Citizen at the Airport", "views": 15140481, "ratio": 24.03},
    {"title": "Police Give Rebellious Woman a Lesson She Won't Forget", "views": 11535273, "ratio": 18.31},
    {"title": "How Officers Find Out He's Got a Body in the House", "views": 10234029, "ratio": 10.57},
    {"title": "Warrant Attempt Turns into Apartment Gunfight", "views": 9490007, "ratio": 12.34},
    {"title": "When a Walmart Shoplifter Thinks She's Entitled to Steal", "views": 9039104, "ratio": 9.34},
    {"title": "Here's Why You Don't Pull a SAW on the Cops", "views": 8937796, "ratio": 11.62},
    {"title": "Teenage Illegal Immigrant Caught Trafficking $700,000 Worth Of Illegal Substance", "views": 8596369, "ratio": 19.99},
    {"title": "Duluth Bodycam is a Real-Life 'Fargo' Movie", "views": 8287808, "ratio": 10.78},
    {"title": "19-Year-Old Doesn't Realize He Just Ended Someone's Life", "views": 8116273, "ratio": 12.88},
    {"title": "The Fake UPS Delivery That Turned Deadly", "views": 8075701, "ratio": 10.5},
    {"title": "Entitled 18-Year-Old Causes Complete Chaos During Arrest", "views": 7610107, "ratio": 12.08},
    {"title": "Police Rescue Family From EXTREMELY Toxic Mother", "views": 7285917, "ratio": 19.8},
    {"title": "How a Restaurant Complaint Becomes an Arrestable Offense", "views": 7135616, "ratio": 7.37},
    {"title": "Employee Arrested for Stealing $25,000 to Purchase a Gucci Purse And New Car", "views": 6669548, "ratio": 15.51},
    {"title": "Disturbed Woman Kicked Off Airplane for Outrageous Behavior", "views": 6663588, "ratio": 10.58},
    {"title": "12-Year-Old Secretly Calls Police on Abusive Father, Doesn't End Well", "views": 4080990, "ratio": 17.82},
    {"title": "World's Most Disrespectful Teen Learns a Lesson in Authority", "views": 4615143, "ratio": 18.68},
    {"title": "Abusive Woman Punches Publix Employee In Front Of Police", "views": 4776021, "ratio": 19.34},
    {"title": "Man Makes His Day 10 Times Worse After Getting Fired from BMW", "views": 3750047, "ratio": 16.38},
]


def _parse_duration(duration_str: str) -> int:
    """Parse ISO 8601 duration (PT1H2M3S) to seconds."""
    if not duration_str:
        return 0
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def get_youtube_transcript(video_id: str) -> str:
    """Get transcript text from a YouTube video."""
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        # Combine all text entries
        full_text = " ".join(entry.text for entry in transcript)
        # Truncate if too long
        if len(full_text) > 15000:
            full_text = full_text[:15000]
        return full_text
    except Exception as e:
        return f"[Transcript unavailable: {e}]"


def analyze_video_with_gemini(video_url: str, gemini_key: str) -> dict:
    """Download video with yt-dlp, upload to Gemini, get full visual + audio analysis."""
    from google import genai
    import subprocess
    import tempfile
    import os
    import time

    video_id = extract_video_id(video_url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    client = genai.Client(api_key=gemini_key)

    # Step 1: Download video with yt-dlp (lowest quality MP4)
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "video.mp4")

    try:
        result = subprocess.run(
            ['yt-dlp', '-f', 'worst[ext=mp4]', '-o', tmp_path,
             f'https://www.youtube.com/watch?v={video_id}'],
            capture_output=True, text=True, timeout=300
        )
        if not os.path.exists(tmp_path):
            # Fallback: any mp4
            subprocess.run(
                ['yt-dlp', '-S', '+size', '--recode-video', 'mp4', '-o', tmp_path,
                 f'https://www.youtube.com/watch?v={video_id}'],
                capture_output=True, text=True, timeout=300
            )
    except FileNotFoundError:
        return {"error": "yt-dlp is not installed. Title Generator requires local setup — run the app on localhost, not Streamlit Cloud."}
    except subprocess.TimeoutExpired:
        return {"error": "Video download timed out (5 min limit)"}

    if not os.path.exists(tmp_path):
        return {"error": f"Failed to download video. Make sure yt-dlp is installed: pip install yt-dlp"}

    file_size_mb = os.path.getsize(tmp_path) / 1024 / 1024

    # Step 2: Upload to Gemini Files API
    try:
        uploaded = client.files.upload(file=tmp_path)

        wait_start = time.time()
        while uploaded.state.name == 'PROCESSING':
            if time.time() - wait_start > 300:
                return {"error": "Gemini video processing timed out"}
            time.sleep(3)
            uploaded = client.files.get(name=uploaded.name)

        if uploaded.state.name != 'ACTIVE':
            return {"error": f"Gemini file processing failed: {uploaded.state.name}"}

    except Exception as e:
        return {"error": f"Gemini upload failed: {e}"}
    finally:
        # Clean up
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    # Step 3: Gemini watches the actual video
    prompt = """You are analyzing a bodycam/crime YouTube video for title generation. Watch the ENTIRE video carefully.

BE EXTREMELY ACCURATE. Only describe what you actually see and hear. Do NOT make anything up.

Provide a detailed breakdown:

1. **WHAT HAPPENED** — Describe the full incident in detail. Who was involved (use real names if mentioned)? What was the crime? What did police do? How did it escalate? How did it end?

2. **KEY DRAMATIC MOMENTS** — What are the most shocking, tense, or emotionally charged moments? Include timestamps if possible.

3. **PEOPLE INVOLVED** — Describe each person: their role (suspect, officer, victim, bystander), name if mentioned, approximate age, gender, behavior, attitude.

4. **SEVERITY LEVEL** — On a scale of 1-10, how shocking/intense is this incident? Why?

5. **CLICKBAIT ANGLES** — What aspects of this story would generate the most curiosity and clicks?
   - What's the most intriguing single detail?
   - What's the "twist" or unexpected element?
   - What emotional reaction will viewers have?

6. **SIMILAR TO** — What type of viral bodycam content does this remind you of?

Return your analysis as JSON:
{"what_happened": "...", "key_moments": ["...", "..."], "people": [{"role": "...", "description": "..."}], "severity": 8, "clickbait_angles": ["...", "..."], "similar_to": "...", "one_line_summary": "..."}
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded, prompt]
        )
        raw = response.text.strip()

        # Clean up uploaded file from Gemini
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            return json.loads(json_match.group())
        return {"error": "No JSON in Gemini response", "raw": raw[:500]}
    except Exception as e:
        return {"error": str(e)}


def search_similar_videos(video_analysis: dict, youtube_key: str, max_results: int = 20) -> list[dict]:
    """Search YouTube for similar bodycam videos to find outlier titles."""
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", developerKey=youtube_key)

    # Build search queries based on the video analysis
    summary = video_analysis.get("one_line_summary", "")
    similar_to = video_analysis.get("similar_to", "")
    clickbait_angles = video_analysis.get("clickbait_angles", [])

    queries = [
        f"bodycam {summary[:50]}",
        f"body cam {similar_to[:50]}",
    ]
    if clickbait_angles:
        queries.append(f"bodycam {clickbait_angles[0][:40]}")

    all_videos = []
    seen_ids = set()

    for query in queries[:3]:
        try:
            search_resp = youtube.search().list(
                part="snippet",
                q=query,
                type="video",
                order="viewCount",
                maxResults=min(max_results, 25),
            ).execute()

            video_ids = []
            for item in search_resp.get("items", []):
                vid = item["id"]["videoId"]
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    video_ids.append(vid)

            if video_ids:
                stats_resp = youtube.videos().list(
                    part="statistics,snippet,contentDetails",
                    id=",".join(video_ids[:25]),
                ).execute()

                for item in stats_resp.get("items", []):
                    # Filter out Shorts and videos under 8 minutes
                    duration = _parse_duration(item.get("contentDetails", {}).get("duration", ""))
                    if duration < 480:  # 8 minutes = 480 seconds
                        continue
                    views = int(item["statistics"].get("viewCount", 0))
                    all_videos.append({
                        "title": item["snippet"]["title"],
                        "views": views,
                        "channel": item["snippet"]["channelTitle"],
                        "video_id": item["id"],
                    })
        except Exception:
            continue

    # Sort by views, return top performers
    all_videos.sort(key=lambda v: v["views"], reverse=True)
    return all_videos[:max_results]


def generate_titles(
    video_analysis: dict,
    similar_titles: list[dict],
    anthropic_key: str,
    num_titles: int = 10,
) -> list[dict]:
    """Use Claude to generate optimized titles based on video analysis and research."""

    # Build the similar titles reference
    similar_ref = "\n".join(
        f"  - \"{t['title']}\" ({t['views']:,} views)"
        for t in similar_titles[:15]
    )

    # Build the top-performing reference
    top_ref = "\n".join(
        f"  - \"{t['title']}\" ({t['views']:,} views, {t['ratio']}x sub ratio)"
        for t in TOP_PERFORMING_TITLES[:15]
    )

    # Build pattern reference
    patterns_ref = json.dumps(TITLE_PATTERNS["structures"], indent=2)

    prompt = f"""You are the world's #1 YouTube title specialist for the BODYCAM / CRIME niche. Your titles consistently generate millions of views. You understand exactly what makes people click.

═══════════════════════════════════════
VIDEO ANALYSIS
═══════════════════════════════════════
{json.dumps(video_analysis, indent=2)}

═══════════════════════════════════════
SIMILAR VIDEOS ON YOUTUBE (top performers for this topic)
═══════════════════════════════════════
{similar_ref}

═══════════════════════════════════════
ALL-TIME TOP PERFORMING BODYCAM TITLES (proven winners)
═══════════════════════════════════════
{top_ref}

═══════════════════════════════════════
PROVEN TITLE STRUCTURES (from research across 10 top bodycam channels)
═══════════════════════════════════════
{patterns_ref}

POWER PHRASES that drive clicks: {', '.join(TITLE_PATTERNS['power_phrases'])}
CHARACTER ARCHETYPES that work: {', '.join(TITLE_PATTERNS['character_archetypes'])}
EMOTIONAL TRIGGERS: {', '.join(TITLE_PATTERNS['emotional_triggers'])}

Optimal title length: {TITLE_PATTERNS['avg_title_length']} characters (range: {TITLE_PATTERNS['optimal_range'][0]}-{TITLE_PATTERNS['optimal_range'][1]})

═══════════════════════════════════════
YOUR TASK
═══════════════════════════════════════

Generate exactly {num_titles} title options for this video. Each title must:

1. Use one of the PROVEN STRUCTURES above (How X, The Moment X, When X, or [Adjective] [Person] [Action])
2. Include at least one POWER PHRASE or EMOTIONAL TRIGGER
3. Create a "curiosity gap" — make the viewer NEED to know what happens
4. Be specific enough to be intriguing but vague enough to require watching
5. Stay within 35-75 characters
6. NEVER reveal the full outcome — tease it
7. Use the most clickable/shocking angle from the video analysis
8. Sound like it belongs on a top bodycam channel

For each title, also provide:
- "structure": which proven structure it uses
- "hook": what curiosity gap or emotional trigger it exploits
- "confidence": 1-10 how likely this is to perform well based on the research data

IMPORTANT RULES:
- Do NOT use "BODYCAM:" or "Body Cam:" as a prefix — the niche audience already knows
- Do NOT use all caps unless it's a single word for emphasis
- Do NOT use clickbait that doesn't match the content — it must be accurate
- DO front-load the most intriguing element
- DO use specific details (ages, dollar amounts, locations) when they add shock value

Return ONLY valid JSON array:
[{{"title": "...", "structure": "...", "hook": "...", "confidence": 8}}, ...]
"""

    client = anthropic.Anthropic(api_key=anthropic_key)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            temperature=0.8,  # Higher temp for creative variety
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Parse JSON array
        json_match = re.search(r'\[[\s\S]*\]', raw)
        if json_match:
            titles = json.loads(json_match.group())
            # Sort by confidence
            titles.sort(key=lambda t: t.get("confidence", 0), reverse=True)
            return titles
        return [{"title": "Error: Could not parse titles", "structure": "", "hook": "", "confidence": 0}]

    except Exception as e:
        return [{"title": f"Error: {e}", "structure": "", "hook": "", "confidence": 0}]


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None
