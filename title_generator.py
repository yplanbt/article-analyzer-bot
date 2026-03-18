"""YouTube Title Generator — bodycam niche specialist using Gemini + Claude Opus."""

import json
import re
import anthropic
from youtube_transcript_api import YouTubeTranscriptApi

# ═══════════════════════════════════════════════════════════════════════════════
# DEEP RESEARCH DATABASE — 1,940 long-form videos across 7 channels
# Channels: Unpopular (646), Vee Cams (170), BodyCamEdition (177),
#           MidwestCrime (134), DailyDoseOfCrime (319), BodyCamWatch (219),
#           CrimeTimeCam (275)
# All 8+ minutes, zero shorts
# ═══════════════════════════════════════════════════════════════════════════════

# Channel-specific DNA — what works for OUR channels
CHANNEL_DNA = {
    "Unpopular": {
        "subs": 368000,
        "total_videos": 646,
        "winning_formula": "Rescue/save narratives involving kids and toxic/dangerous parents",
        "best_opener": "Cops (1.84x avg) > Police (0.95x avg)",
        "top_keywords": {
            "Rescue": {"count": 64, "avg_ratio": 2.01, "max_ratio": 19.80},
            "Insane/Insanely": {"count": 5, "avg_ratio": 2.46, "max_ratio": 7.64},
            "Nightmare": {"count": 7, "avg_ratio": 2.21, "max_ratio": 7.45},
            "Dangerous": {"count": 18, "avg_ratio": 1.33, "max_ratio": 7.64},
            "Kids/Children": {"count": 125, "avg_ratio": 1.29, "max_ratio": 9.16},
            "EXTREMELY": {"count": 46, "avg_ratio": 1.29, "max_ratio": 19.80},
            "Toxic": {"count": 58, "avg_ratio": 1.03, "max_ratio": 19.80},
        },
        "underperforming_keywords": ["Karen (0.39x)", "Drunk (0.34x)", "Man (0.39x)"],
        "optimal_length": "45-65 characters (0.85x avg vs 0.65x for 65+)",
        "viral_titles": [
            {"title": "Police Rescue Family From EXTREMELY Toxic Mother", "ratio": 19.8},
            {"title": "Police Save Kids From a Teen Who Terrorized the Neighbourhood", "ratio": 9.16},
            {"title": "Cops RESCUE Kid From a Terrifying House of Horrors", "ratio": 8.74},
            {"title": "Little Girl's 911 Call Rescues Her From INSANELY Dangerous Mother", "ratio": 7.64},
            {"title": "Cops RESCUE 5 Kids From a Living Nightmare Inside Their Home", "ratio": 7.45},
            {"title": "Police Discover Dr*gs in 5-Year-Old's System, Mom Gets in BIG Trouble", "ratio": 6.87},
            {"title": "20-Year-Old's Shoplifting Spree Ends In Worst Way Possible", "ratio": 6.75},
            {"title": "Kid Calls Cops After His Mom Passes Out Doing Drugs", "ratio": 5.84},
            {"title": "Millionaire HOA Boss Fights Neighbor, Then Gets Reality Check", "ratio": 5.54},
            {"title": "Woman Loses It When Facebook Date Refuses to Let Her Stay the Night", "ratio": 5.28},
        ],
    },
    "Vee Cams": {
        "subs": 45400,
        "total_videos": 170,
        "winning_formula": "Punchy, specific hooks with dramatic reveals — uses censoring asterisks and 'Ends Badly'",
        "best_opener": "Specific detail openers (Son's, 4-Year-Old's, Karen)",
        "viral_titles": [
            {"title": "Police Rescue Kids as TEEN Terrorizes Neighborhood", "ratio": 24.3},
            {"title": "Cops Find 3 Kids Sleeping in Road While Mom at Nightclub", "ratio": 22.46},
            {"title": "Terrified Mom Rescues 3 Kids From Granny's Toxic Business", "ratio": 22.32},
            {"title": "Son's Snapchat Texts Lead Cops to Mom's Hidden Nightmare", "ratio": 12.05},
            {"title": "Karen Refuses To Leave Walmart& All Hell Breaks Loose", "ratio": 12.03},
            {"title": "4-Year-Old's 911 Call Exposes Karen's House Of Horror", "ratio": 11.46},
            {"title": "Airport Karen Loses Control& Attacks Officers Mid-Flight", "ratio": 9.58},
            {"title": "Neglectful Mom Gets Drunk While Her Child Is Freezing Outside * Ends Badly *", "ratio": 8.7},
            {"title": "Mom With 11yo Wrecks School Bus& Her World Collapses", "ratio": 7.82},
            {"title": "Couple Laughs At Cops Until The Safe Pops Open", "ratio": 7.8},
        ],
    },
}

# Top performing competitor titles for reference
COMPETITOR_TOP_TITLES = [
    {"title": "The Moment She Realized She Killed 2 People", "views": 20409867, "ratio": 26.54, "channel": "Midwest Crime"},
    {"title": "Racist Woman Arrested After Attacking Senior Citizen at the Airport", "views": 15140538, "ratio": 24.03, "channel": "Body Cam Edition"},
    {"title": "Police Give Rebellious Woman a Lesson She Won't Forget", "views": 11535643, "ratio": 18.31, "channel": "Body Cam Edition"},
    {"title": "How Officers Find Out He's Got a Body in the House", "views": 10234142, "ratio": 10.57, "channel": "Body Cam Watch"},
    {"title": "Warrant Attempt Turns into Apartment Gunfight", "views": 9490398, "ratio": 12.34, "channel": "Midwest Crime"},
    {"title": "When a Walmart Shoplifter Thinks She's Entitled to Steal", "views": 9039255, "ratio": 9.34, "channel": "Body Cam Watch"},
    {"title": "Here's Why You Don't Pull a SAW on the Cops", "views": 8937957, "ratio": 11.62, "channel": "Midwest Crime"},
    {"title": "19-Year-Old Doesn't Realize He Just Ended Someone's Life", "views": 8116314, "ratio": 12.88, "channel": "Body Cam Edition"},
    {"title": "The Fake UPS Delivery That Turned Deadly", "views": 8075762, "ratio": 10.5, "channel": "Midwest Crime"},
    {"title": "Entitled 18-Year-Old Causes Complete Chaos During Arrest", "views": 7610300, "ratio": 12.08, "channel": "Body Cam Edition"},
    {"title": "Wandering Toddler Leads Police to His Irresponsible Mom", "views": 6133902, "ratio": 7.69, "channel": "Daily Dose Of Crime"},
    {"title": "Abusive Woman Punches Publix Employee In Front Of Police", "views": 4776021, "ratio": 19.34, "channel": "Crime Time Cam"},
    {"title": "World's Most Disrespectful Teen Learns a Lesson in Authority", "views": 4615287, "ratio": 18.69, "channel": "Crime Time Cam"},
    {"title": "Cops Rescue 7 Children After Disturbing Traffic Stop", "views": 4796185, "ratio": 6.01, "channel": "Daily Dose Of Crime"},
    {"title": "12-Year-Old Secretly Calls Police on Abusive Father, Doesn't End Well", "views": 4080990, "ratio": 17.82, "channel": "Police Release"},
]

# Niche-wide proven patterns
PROVEN_PATTERNS = {
    "structures": {
        "Cops/Police [Action] [Person] From [Situation]": {
            "avg_ratio": 2.01, "best_for": "Unpopular",
            "examples": [
                "Police Rescue Family From EXTREMELY Toxic Mother",
                "Cops RESCUE Kid From a Terrifying House of Horrors",
                "Cops RESCUE 5 Kids From a Living Nightmare Inside Their Home",
            ],
        },
        "[Specific Detail] Leads/Exposes [Dramatic Reveal]": {
            "avg_ratio": 1.8, "best_for": "Vee Cams",
            "examples": [
                "Son's Snapchat Texts Lead Cops to Mom's Hidden Nightmare",
                "4-Year-Old's 911 Call Exposes Karen's House Of Horror",
                "Little Girl's 911 Call Rescues Her From INSANELY Dangerous Mother",
            ],
        },
        "How X Turns into Y": {
            "avg_ratio": 1.2, "best_for": "Body Cam Watch",
            "examples": [
                "How Officers Find Out He's Got a Body in the House",
                "How a Driveway Confrontation Turns a Citation into an Arrest",
            ],
        },
        "The Moment [Person] Realized [Consequence]": {
            "avg_ratio": 1.5, "best_for": "Midwest Crime",
            "examples": [
                "The Moment She Realized She Killed 2 People",
                "19-Year-Old Doesn't Realize He Just Ended Someone's Life",
            ],
        },
        "[Person] [Action] & [Dramatic Consequence]": {
            "avg_ratio": 1.6, "best_for": "Vee Cams",
            "examples": [
                "Karen Refuses To Leave Walmart& All Hell Breaks Loose",
                "Mom With 11yo Wrecks School Bus& Her World Collapses",
                "Couple Laughs At Cops Until The Safe Pops Open",
            ],
        },
    },
    "high_performing_keywords": [
        "RESCUE (2.01x)", "Nightmare (2.21x)", "Insane/Insanely (2.46x)",
        "EXTREMELY (1.29x)", "Toxic (1.03x)", "Kids/Children (1.29x)",
        "Ends Badly", "House of Horrors", "Living Nightmare",
        "Gets in BIG Trouble", "All Hell Breaks Loose", "Worst Way Possible",
    ],
    "low_performing_avoid": [
        "Karen (0.39x on Unpopular)", "Drunk (0.34x)", "Man (0.39x on Unpopular)",
        "When (0.51x opener)", "Woman's (0.38x opener)",
    ],
    "optimal_length": {"min": 45, "max": 65, "avg": 56},
    "censoring_trick": "Use asterisks (Dr*gs, K*ll, Vi*lent) — creates curiosity + safe for algorithm",
}


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
        full_text = " ".join(entry.text for entry in transcript)
        if len(full_text) > 15000:
            full_text = full_text[:15000]
        return full_text
    except Exception as e:
        return f"[Transcript unavailable: {e}]"


def _get_video_metadata(video_id: str, youtube_key: str) -> dict:
    """Get video title, description, and tags from YouTube API."""
    import requests as req

    # Use REST API directly to avoid googleapiclient issues on cloud
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet", "id": video_id, "key": youtube_key}
    try:
        resp = req.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("items"):
                snippet = data["items"][0]["snippet"]
                return {
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", "")[:2000],
                    "tags": snippet.get("tags", []),
                    "channel": snippet.get("channelTitle", ""),
                    "_error": None,
                }
        return {"_error": f"YouTube API {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"_error": f"YouTube API error: {e}"}


def _build_analysis_prompt(transcript: str, metadata: dict) -> str:
    """Build the analysis prompt for Gemini."""
    return f"""You are analyzing a bodycam/crime YouTube video for title generation.

CRITICAL: You can ONLY use the transcript/dialogue. You CANNOT see the video visuals.
- ONLY describe what is explicitly stated in dialogue/narration
- Do NOT imagine or hallucinate ANY visual details
- Use EXACT quotes and names from the transcript
- If something is unclear, say so — do NOT guess

VIDEO METADATA:
- Original title: {metadata.get('title', 'Unknown')}
- Channel: {metadata.get('channel', 'Unknown')}
- Description: {metadata.get('description', 'N/A')[:500]}

FULL TRANSCRIPT:
{transcript}

Based STRICTLY on the transcript, provide:

1. **WHAT HAPPENED** — Full incident description using ONLY transcript information. Names, charges, what police did, how it ended.

2. **KEY DRAMATIC MOMENTS** — Most shocking/tense dialogue moments. Quote exact words.

3. **PEOPLE INVOLVED** — Each person: role, name if stated, gender, age if mentioned, behavior/attitude from their dialogue.

4. **SEVERITY** — 1-10 based on what's described.

5. **CLICKBAIT ANGLES** — What aspects drive the most curiosity?

6. **SIMILAR TO** — What type of viral bodycam content is this?

Return ONLY valid JSON:
{{"what_happened": "...", "key_moments": ["...", "..."], "people": [{{"role": "...", "description": "..."}}], "severity": 8, "clickbait_angles": ["...", "..."], "similar_to": "...", "one_line_summary": "..."}}
"""


def _call_gemini_rest(api_key: str, prompt: str) -> dict:
    """Call Gemini API via REST (text-only)."""
    import requests as req

    base = "https://generativelanguage.googleapis.com"
    url = f"{base}/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    resp = req.post(url, json=payload, timeout=180)
    if resp.status_code != 200:
        return {"error": f"{resp.status_code}: {resp.text[:500]}"}

    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if json_match:
        return json.loads(json_match.group())
    return {"error": "No JSON in Gemini response", "raw": raw[:500]}


def analyze_video_with_gemini(video_url: str, gemini_key: str, youtube_key: str = None) -> dict:
    """Analyze video using transcript + metadata with strict accuracy prompting.

    Tries youtube-transcript-api first. If that fails (e.g. cloud IP blocked),
    falls back to metadata-only analysis.
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    metadata = _get_video_metadata(video_id, youtube_key) if youtube_key else {}

    # Try getting transcript via library first
    transcript = get_youtube_transcript(video_id)
    transcript_available = not transcript.startswith("[Transcript unavailable")

    if transcript_available:
        # Have transcript — send as text
        prompt = _build_analysis_prompt(transcript, metadata)
        try:
            return _call_gemini_rest(gemini_key, prompt)
        except Exception as e:
            return {"error": str(e)}
    else:
        # Transcript library blocked — use metadata-only analysis
        if metadata.get("_error") or not metadata.get("title"):
            return {"error": f"Transcript unavailable on cloud and YouTube metadata fetch failed: {metadata.get('_error', 'no metadata')}. Check your YouTube API Key."}

        fallback_prompt = f"""You are analyzing a bodycam/crime YouTube video for title generation.
The transcript is unavailable, so analyze based on the metadata below. Use the title and description to infer as much as possible about the incident.

VIDEO METADATA:
- Original title: {metadata.get('title', 'Unknown')}
- Channel: {metadata.get('channel', 'Unknown')}
- Tags: {', '.join(metadata.get('tags', [])[:20])}
- Full description:
{metadata.get('description', 'N/A')}

Based on the metadata, provide your best analysis:

1. **WHAT HAPPENED** — Infer the full incident from the title and description. Names, charges, what police did, how it ended.
2. **KEY DRAMATIC MOMENTS** — Most likely dramatic moments based on what's described.
3. **PEOPLE INVOLVED** — Each person mentioned: role, name if stated, behavior/attitude.
4. **SEVERITY** — 1-10 based on what happened.
5. **CLICKBAIT ANGLES** — What aspects drive the most curiosity?
6. **SIMILAR TO** — What type of viral bodycam content is this?

Return ONLY valid JSON:
{{"what_happened": "...", "key_moments": ["...", "..."], "people": [{{"role": "...", "description": "..."}}], "severity": 8, "clickbait_angles": ["...", "..."], "similar_to": "...", "one_line_summary": "..."}}
"""
        try:
            return _call_gemini_rest(gemini_key, fallback_prompt)
        except Exception as e:
            return {"error": str(e)}


def search_similar_videos(video_analysis: dict, youtube_key: str, max_results: int = 20) -> list[dict]:
    """Search YouTube for similar bodycam videos to find outlier titles."""
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", developerKey=youtube_key)

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
                part="snippet", q=query, type="video",
                order="viewCount", maxResults=min(max_results, 25),
            ).execute()

            video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])
                         if item["id"]["videoId"] not in seen_ids]
            seen_ids.update(video_ids)

            if video_ids:
                stats_resp = youtube.videos().list(
                    part="statistics,snippet,contentDetails",
                    id=",".join(video_ids[:25]),
                ).execute()

                for item in stats_resp.get("items", []):
                    duration = _parse_duration(item.get("contentDetails", {}).get("duration", ""))
                    if duration < 480:
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

    all_videos.sort(key=lambda v: v["views"], reverse=True)
    return all_videos[:max_results]


def generate_titles(
    video_analysis: dict,
    similar_titles: list[dict],
    anthropic_key: str,
    num_titles: int = 10,
    target_channel: str = "Unpopular",
) -> list[dict]:
    """Use Claude Opus to generate optimized titles based on deep channel research."""

    # Get channel-specific DNA
    channel_dna = CHANNEL_DNA.get(target_channel, CHANNEL_DNA["Unpopular"])

    # Build channel-specific viral titles reference
    channel_viral = "\n".join(
        f"  - \"{t['title']}\" ({t['ratio']}x sub ratio)"
        for t in channel_dna["viral_titles"]
    )

    # Build competitor reference
    competitor_ref = "\n".join(
        f"  - \"{t['title']}\" ({t['views']:,} views, {t['ratio']}x ratio) [{t['channel']}]"
        for t in COMPETITOR_TOP_TITLES[:10]
    )

    # Build similar titles reference
    similar_ref = "\n".join(
        f"  - \"{t['title']}\" ({t['views']:,} views)"
        for t in similar_titles[:10]
    )

    # Build patterns reference
    patterns_ref = ""
    for name, data in PROVEN_PATTERNS["structures"].items():
        examples = "\n".join(f"      \"{e}\"" for e in data["examples"])
        patterns_ref += f"\n  {name} (avg {data['avg_ratio']}x, best for {data['best_for']}):\n{examples}\n"

    prompt = f"""You are the title strategist for "{target_channel}", a bodycam/crime YouTube channel. You have deep knowledge of what works on this specific channel based on data from {channel_dna['total_videos']} videos.

═══════════════════════════════════════
CHANNEL INTELLIGENCE: {target_channel}
═══════════════════════════════════════
Subscribers: {channel_dna['subs']:,}
Winning formula: {channel_dna['winning_formula']}
Best opener: {channel_dna['best_opener']}
{f"Top keywords: " + ", ".join(f"{k} ({v['avg_ratio']}x)" for k, v in channel_dna.get('top_keywords', {}).items())}
{f"AVOID these (underperform): " + ", ".join(channel_dna.get('underperforming_keywords', [])) if channel_dna.get('underperforming_keywords') else ""}

{target_channel}'s PROVEN VIRAL TITLES:
{channel_viral}

═══════════════════════════════════════
VIDEO TO TITLE
═══════════════════════════════════════
{json.dumps(video_analysis, indent=2)}

═══════════════════════════════════════
SIMILAR VIRAL VIDEOS ON THIS TOPIC
═══════════════════════════════════════
{similar_ref}

═══════════════════════════════════════
COMPETITOR MEGA-HITS (what goes viral across the niche)
═══════════════════════════════════════
{competitor_ref}

═══════════════════════════════════════
PROVEN TITLE STRUCTURES (ranked by performance)
═══════════════════════════════════════
{patterns_ref}

HIGH-PERFORMING KEYWORDS: {", ".join(PROVEN_PATTERNS["high_performing_keywords"])}
AVOID (underperform): {", ".join(PROVEN_PATTERNS["low_performing_avoid"])}
Optimal length: {PROVEN_PATTERNS["optimal_length"]["min"]}-{PROVEN_PATTERNS["optimal_length"]["max"]} chars
Censoring trick: {PROVEN_PATTERNS["censoring_trick"]}

═══════════════════════════════════════
YOUR TASK
═══════════════════════════════════════

Generate exactly {num_titles} titles for this video that would perform on {target_channel}.

RULES:
1. Study {target_channel}'s viral titles above — your titles MUST feel like they belong on this channel
2. Use the channel's winning formula and best-performing keywords
3. Avoid the channel's underperforming keywords
4. Stay within 45-65 characters (the sweet spot)
5. Create a curiosity gap — make the viewer NEED to watch
6. Be specific (ages, numbers, details) but don't reveal the outcome
7. NEVER use "BODYCAM:" prefix
8. Use ALL CAPS sparingly — max ONE word (RESCUE, EXTREMELY, INSANELY)
9. Consider using asterisks for sensitive words (Dr*gs, K*ll) — it works on Vee Cams
10. The title must accurately represent the video content

For each title provide:
- "title": the title
- "structure": which proven structure it uses
- "hook": the curiosity gap / emotional trigger
- "confidence": 1-10 predicted performance based on channel data
- "reasoning": one sentence on why this would work for {target_channel}

Return ONLY valid JSON array:
[{{"title": "...", "structure": "...", "hook": "...", "confidence": 8, "reasoning": "..."}}]
"""

    client = anthropic.Anthropic(api_key=anthropic_key)

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=3000,
            temperature=0.8,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        json_match = re.search(r'\[[\s\S]*\]', raw)
        if json_match:
            titles = json.loads(json_match.group())
            titles.sort(key=lambda t: t.get("confidence", 0), reverse=True)
            return titles
        return [{"title": "Error: Could not parse titles", "structure": "", "hook": "", "confidence": 0, "reasoning": ""}]

    except Exception as e:
        return [{"title": f"Error: {e}", "structure": "", "hook": "", "confidence": 0, "reasoning": ""}]


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
