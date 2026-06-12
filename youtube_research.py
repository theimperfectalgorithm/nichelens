#!/usr/bin/env python3
"""YouTube Research Tool — competitor analysis & title intelligence."""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import isodate
import pandas as pd
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    print("ERROR: YOUTUBE_API_KEY not set. Copy .env.example to .env and add your key.")
    sys.exit(1)

MY_CHANNEL_ID = os.getenv("MY_CHANNEL_ID", "").strip()

# ---------------------------------------------------------------------------
# Config — edit these to customise the tool
# ---------------------------------------------------------------------------

KEYWORDS = [
    "AI trading bot",
    "forex bot python",
    "algorithmic trading beginner",
    "claude AI coding",
    "build in public",
    "metatrader5 python",
    "forex trading bot",
]

MY_TITLES = [
    "I Asked AI to Build a Forex Bot. Here's What Actually Happened.",
    "I Let AI Build a Forex Strategy. It Found a Death Cross.",
    "I Connected an AI Bot to a Live Trading Platform. Here's What Happened.",
    "Our Bot Loses 6 Out of 10 Trades. Still Made Money.",
    "We Added RSI. It Made Things Worse.",
    "I Fixed My RSI Settings. Plus $388 Profit. 2026.",
]

MAX_RESULTS = 20
SUBSCRIBER_LIMIT = 500_000
MIN_DURATION_SECONDS = 180          # exclude Shorts (< 3 min)
RECENT_DAYS = 365                   # "recent" threshold for video age flag
COMPETITOR_VIEW_GROWTH_PCT = 20     # flag videos with ≥20% view growth
MAX_CHANNEL_DIVE = 10               # max competitor channels for deep dive (quota protection)

DATA_DIR = Path("data")
THUMBNAILS_DIR = Path("thumbnails")
CLIENT_SECRET_FILE = Path("client_secret.json")
TOKEN_FILE = Path("token.json")
ANALYTICS_SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]

# ---------------------------------------------------------------------------
# Stop words & sentiment rules
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "i", "my",
    "your", "you", "we", "he", "she", "how", "what", "why", "when",
    "where", "do", "did", "can", "will", "get", "got", "make", "made",
    "use", "using", "vs", "not", "be", "have", "has", "had", "are",
    "was", "were", "so", "as", "just", "if", "no", "yes", "all", "its",
    "their", "our", "new", "more", "up", "out", "about", "like", "than",
    "into", "am", "me", "us", "him", "her", "they", "them", "very",
    "here", "which", "still", "plus", "let", "added", "asked", "fixed",
    "lost", "connected", "built", "tried", "thing", "things", "way",
}

# Evaluated in order; first match wins
SENTIMENT_RULES = [
    ("Question",  [r"\?", r"^(what|why|which|is|does|can|should|will|are|do)\b"]),
    ("How-To",    [r"\bhow\s+to\b", r"\btutorial\b", r"\bguide\b", r"\bstep.by.step\b",
                   r"\bbeginner[s]?\b", r"\bcomplete\b.*\bcourse\b", r"\bfor\s+beginners\b"]),
    ("Fear",      [r"\bmistake[s]?\b", r"\bwarning\b", r"\bavoid\b", r"\bstop\b",
                   r"\bwrong\b", r"\brisk\b", r"\blosin\b", r"\bfailed\b", r"\bdanger\b",
                   r"\bnever\b", r"\bscam\b"]),
    ("Curiosity", [r"\bsecret\b", r"\bhidden\b", r"\brevealed?\b", r"\bnobody\b",
                   r"\bactually\b", r"\breally\b", r"won.t believe", r"\bsurpris\b",
                   r"\btruth\b", r"\breal\b.*\b(results?|story)\b"]),
    ("Results",   [r"\$\d+", r"\d+[km]\b", r"\bprofit\b", r"\bearned\b", r"\bmade\b.*\$",
                   r"\btripled\b", r"\bdoubled\b", r"\bmoney\b", r"\brich\b", r"\bwork[s]?\b"]),
    ("Story",     [r"\bi\s+(tried|built|let|created|made|used|asked|connected|fixed)\b",
                   r"\bday\s+\d+\b", r"\bjourney\b", r"\bmy\s+(bot|trading|strategy|story)\b",
                   r"\bwe\s+(added|built|tried|tested)\b"]),
]

SENTIMENT_STRENGTH = {"How-To": 2, "Results": 2, "Curiosity": 2,
                       "Story": 1, "Fear": 1, "Question": 1, "General": 0}

# ===========================================================================
# API helpers
# ===========================================================================

def youtube_client():
    return build("youtube", "v3", developerKey=API_KEY)


def search_videos(yt, keyword, max_results=MAX_RESULTS):
    ids = []
    token = None
    while len(ids) < max_results:
        resp = yt.search().list(
            part="id",
            q=keyword,
            type="video",
            maxResults=min(max_results - len(ids), 50),
            order="relevance",
            pageToken=token,
        ).execute()
        for item in resp.get("items", []):
            ids.append(item["id"]["videoId"])
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:max_results]


def fetch_video_details(yt, video_ids):
    details = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = yt.videos().list(
            part="snippet,statistics,contentDetails",
            id=",".join(batch),
        ).execute()

        for item in resp.get("items", []):
            vid_id = item["id"]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})

            duration_secs, duration_str = _parse_duration(content.get("duration", "PT0S"))
            published_at = snippet.get("publishedAt", "")

            upload_dt = None
            if published_at:
                try:
                    upload_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except ValueError:
                    pass

            thumbs = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumbs.get("maxres", {}).get("url")
                or thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url", "")
            )

            details[vid_id] = {
                "video_id": vid_id,
                "title": snippet.get("title", ""),
                "channel_id": snippet.get("channelId", ""),
                "channel_name": snippet.get("channelTitle", ""),
                "upload_date": published_at[:10] if published_at else "",
                "upload_datetime": published_at,
                "upload_day_of_week": upload_dt.strftime("%A") if upload_dt else "",
                "upload_hour_utc": upload_dt.hour if upload_dt else -1,
                "duration": duration_str,
                "duration_seconds": duration_secs,
                "view_count": _safe_int(stats.get("viewCount")),
                "like_count": _safe_int(stats.get("likeCount")),
                "comment_count": _safe_int(stats.get("commentCount")),
                "thumbnail_url": thumbnail_url,
            }
    return details


def fetch_subscriber_counts(yt, channel_ids):
    counts = {}
    unique = list(set(channel_ids))
    for i in range(0, len(unique), 50):
        batch = unique[i:i + 50]
        resp = yt.channels().list(part="statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            raw = item.get("statistics", {}).get("subscriberCount")
            counts[item["id"]] = _safe_int(raw)
    return counts


# ===========================================================================
# Core research pipeline
# ===========================================================================

def run_research(yt):
    """Search, filter, deduplicate. Returns (all_results, master)."""
    all_results = {}
    master = {}  # video_id -> video dict; accumulates keywords across searches

    for keyword in KEYWORDS:
        print(f'  Searching "{keyword}" ...', end=" ", flush=True)
        try:
            video_ids = search_videos(yt, keyword)
            if not video_ids:
                print("no results.")
                all_results[keyword] = []
                continue

            details = fetch_video_details(yt, video_ids)
            sub_counts = fetch_subscriber_counts(yt, [v["channel_id"] for v in details.values()])

            filtered = []
            shorts_dropped = 0
            for video in details.values():
                subs = sub_counts.get(video["channel_id"], 0)
                if subs > SUBSCRIBER_LIMIT:
                    continue
                if video["duration_seconds"] < MIN_DURATION_SECONDS:
                    shorts_dropped += 1
                    continue
                video["subscriber_count"] = subs
                filtered.append(video)

                if video["video_id"] in master:
                    master[video["video_id"]]["_keywords"].append(keyword)
                else:
                    video["_keywords"] = [keyword]
                    master[video["video_id"]] = video

            print(f"{len(video_ids)} found → {len(filtered)} kept  ({shorts_dropped} Shorts removed)")
            all_results[keyword] = filtered

        except HttpError as exc:
            print(f"API error — {exc}")
            all_results[keyword] = []

    # Post-process master
    channel_kw_counts = Counter(v["channel_id"] for v in master.values())
    today = datetime.now(timezone.utc).date()

    for video in master.values():
        video["keywords"] = ", ".join(video["_keywords"])
        video["channel_keyword_count"] = channel_kw_counts[video["channel_id"]]
        video["is_competitor"] = video["channel_keyword_count"] > 1
        video["sentiment"] = classify_sentiment(video["title"])

        upload_date = video.get("upload_date", "")
        if upload_date:
            try:
                age = (today - datetime.strptime(upload_date, "%Y-%m-%d").date()).days
                video["video_age_days"] = age
                video["is_recent"] = age <= RECENT_DAYS
            except ValueError:
                video["video_age_days"] = None
                video["is_recent"] = None
        else:
            video["video_age_days"] = None
            video["is_recent"] = None

    return all_results, master


# ===========================================================================
# Category 1: Competitor analysis
# ===========================================================================

def download_thumbnails(all_results):
    """Download top-5 thumbnails per keyword into thumbnails/<keyword>/ folders."""
    print("\n  Downloading thumbnails...")
    downloaded = failed = skipped = 0

    for keyword, videos in all_results.items():
        if not videos:
            continue
        folder_name = re.sub(r"[^\w\s-]", "", keyword).strip().replace(" ", "_")
        folder = THUMBNAILS_DIR / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        top5 = sorted(videos, key=lambda v: v["view_count"], reverse=True)[:5]
        for rank, video in enumerate(top5, 1):
            url = video.get("thumbnail_url", "")
            if not url:
                skipped += 1
                continue
            safe_title = re.sub(r"[^\w\s-]", "", video["title"][:40]).strip().replace(" ", "_")
            dest = folder / f"{rank:02d}_{safe_title}.jpg"
            if dest.exists():
                skipped += 1
                continue
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                dest.write_bytes(r.content)
                downloaded += 1
            except Exception:
                failed += 1

    print(f"  Downloaded: {downloaded}  |  Skipped (already exist): {skipped}  |  Failed: {failed}")
    print(f"  Location: {THUMBNAILS_DIR}/")


def download_thumbnails_from_csv(csv_path):
    """Read a research CSV and download every thumbnail into thumbnails/<keyword>/<channel>_<id>.jpg."""
    path = Path(csv_path)
    if not path.exists():
        print(f"  File not found: {csv_path}")
        return

    df = pd.read_csv(path)
    required = {"keywords", "channel_name", "video_id", "thumbnail_url"}
    if not required.issubset(df.columns):
        missing = required - set(df.columns)
        print(f"  CSV is missing columns: {missing}")
        return

    print(f"\n  Reading {len(df)} rows from {path.name} ...")

    downloaded = failed = skipped = 0
    seen_video_ids: set = set()  # each video saved once (first keyword only)

    for _, row in df.iterrows():
        vid_id = str(row.get("video_id", "")).strip()
        url = str(row.get("thumbnail_url", "")).strip()
        channel = str(row.get("channel_name", "unknown")).strip()
        # Take the first keyword when a video spans multiple
        keywords_raw = str(row.get("keywords", "")).strip()
        keyword = keywords_raw.split(",")[0].strip() if keywords_raw else "unknown"

        if not vid_id or not url or url == "nan":
            skipped += 1
            continue

        if vid_id in seen_video_ids:
            skipped += 1
            continue
        seen_video_ids.add(vid_id)

        folder_name = re.sub(r"[^\w\s-]", "", keyword).strip().replace(" ", "_")
        safe_channel = re.sub(r"[^\w\s-]", "", channel).strip().replace(" ", "_")[:40]
        folder = THUMBNAILS_DIR / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        dest = folder / f"{safe_channel}_{vid_id}.jpg"
        if dest.exists():
            skipped += 1
            continue

        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            dest.write_bytes(r.content)
            downloaded += 1
        except Exception as exc:
            print(f"    FAIL {vid_id}: {exc}")
            failed += 1

    print(f"\n  Done.")
    print(f"    Downloaded : {downloaded}")
    print(f"    Skipped    : {skipped}  (already existed or no URL)")
    print(f"    Failed     : {failed}")
    print(f"    Location   : {THUMBNAILS_DIR}/")


def channel_deep_dive(yt, master):
    """Pull last 10 videos + upload frequency for each repeat-competitor channel."""
    competitors = {
        v["channel_id"]: v["channel_name"]
        for v in master.values()
        if v.get("channel_keyword_count", 1) > 1
    }
    if not competitors:
        print("\n  No repeat competitor channels found.")
        return []

    # Sort by keyword count desc, cap to protect quota
    kw_count = Counter(v["channel_id"] for v in master.values())
    ordered = sorted(competitors.keys(), key=lambda x: -kw_count[x])[:MAX_CHANNEL_DIVE]

    print(f"\n  Channel deep dive — {len(ordered)} competitor channel(s)...")
    rows = []

    for channel_id in ordered:
        ch_name = competitors[channel_id]
        print(f"    {ch_name} ...", end=" ", flush=True)
        try:
            resp = yt.search().list(
                part="id",
                channelId=channel_id,
                type="video",
                order="date",
                maxResults=10,
            ).execute()
            vid_ids = [item["id"]["videoId"] for item in resp.get("items", [])]
            if not vid_ids:
                print("no videos found.")
                continue

            details = fetch_video_details(yt, vid_ids)
            sorted_vids = sorted(details.values(), key=lambda v: v["upload_date"], reverse=True)

            # Upload frequency: average days between consecutive uploads
            dates = sorted(
                [datetime.strptime(v["upload_date"], "%Y-%m-%d")
                 for v in sorted_vids if v["upload_date"]],
                reverse=True,
            )
            if len(dates) >= 2:
                gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
                avg_gap = round(sum(gaps) / len(gaps), 1)
            else:
                avg_gap = "N/A"

            print(f"{len(sorted_vids)} videos, avg upload every {avg_gap} days")

            for v in sorted_vids:
                rows.append({
                    "channel_name": ch_name,
                    "channel_id": channel_id,
                    "keywords_count": kw_count[channel_id],
                    "avg_days_between_uploads": avg_gap,
                    "video_title": v["title"],
                    "view_count": v["view_count"],
                    "upload_date": v["upload_date"],
                    "duration": v["duration"],
                })

        except HttpError as exc:
            print(f"API error — {exc}")

    return rows


def upload_timing_analysis(master):
    """Show which upload day/hour correlates with highest views. Returns (day_rows, hour_rows)."""
    DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_views: dict = defaultdict(list)
    hour_views: dict = defaultdict(list)

    for video in master.values():
        day = video.get("upload_day_of_week", "")
        hour = video.get("upload_hour_utc", -1)
        views = video.get("view_count", 0)
        if day:
            day_views[day].append(views)
        if isinstance(hour, int) and hour >= 0:
            hour_views[hour].append(views)

    day_rows = []
    for day in DAY_ORDER:
        vl = day_views.get(day, [])
        if vl:
            day_rows.append({"day": day, "video_count": len(vl),
                             "avg_views": int(sum(vl) / len(vl)), "total_views": sum(vl)})

    hour_rows = []
    for hour in range(24):
        vl = hour_views.get(hour, [])
        if vl:
            hour_rows.append({"hour_utc": hour, "hour_label": f"{hour:02d}:00 UTC",
                              "video_count": len(vl), "avg_views": int(sum(vl) / len(vl))})

    print("\n  Upload Day Analysis (avg views, sorted best → worst):")
    max_avg = max((r["avg_views"] for r in day_rows), default=1)
    for r in sorted(day_rows, key=lambda x: -x["avg_views"]):
        bar = "█" * int(20 * r["avg_views"] / max_avg)
        print(f"    {r['day']:<10} {bar:<20} {r['avg_views']:>10,} avg views  ({r['video_count']} videos)")

    print("\n  Best upload hours UTC (top 5):")
    for r in sorted(hour_rows, key=lambda x: -x["avg_views"])[:5]:
        print(f"    {r['hour_label']}  →  {r['avg_views']:>10,} avg views  ({r['video_count']} videos)")

    return day_rows, hour_rows


# ===========================================================================
# Category 2: Title & keyword intelligence
# ===========================================================================

def classify_sentiment(title):
    t = title.lower()
    for category, patterns in SENTIMENT_RULES:
        for pattern in patterns:
            if re.search(pattern, t):
                return category
    return "General"


def sentiment_analysis(all_results):
    """Show which sentiment category averages the most views per keyword."""
    print("\n  Sentiment Analysis by Keyword:")
    rows = []

    for keyword, videos in all_results.items():
        if not videos:
            continue
        cat_data: dict = defaultdict(list)
        for v in videos:
            cat = classify_sentiment(v["title"])
            cat_data[cat].append(v["view_count"])
            rows.append({
                "keyword": keyword,
                "title": v["title"],
                "sentiment": cat,
                "view_count": v["view_count"],
                "channel_name": v["channel_name"],
                "upload_date": v["upload_date"],
            })

        print(f"\n    \"{keyword}\":")
        for cat, vl in sorted(cat_data.items(), key=lambda x: -(sum(x[1]) / len(x[1]))):
            print(f"      {cat:<12} {len(vl):>2} videos   avg {int(sum(vl)/len(vl)):>10,} views")

    return rows


def keyword_gap_finder(master, all_results):
    """Flag high-performing keywords in top videos that don't appear in MY_TITLES."""
    my_words: set = set()
    for title in MY_TITLES:
        for w in re.findall(r"\b[a-zA-Z]+\b", title.lower()):
            if w not in STOP_WORDS and len(w) > 2:
                my_words.add(w)

    # Collect words from top-25% videos per keyword
    word_views: dict = defaultdict(list)
    for keyword, videos in all_results.items():
        if not videos:
            continue
        top_n = max(1, len(videos) // 4)
        for v in sorted(videos, key=lambda x: x["view_count"], reverse=True)[:top_n]:
            for w in re.findall(r"\b[a-zA-Z]+\b", v["title"].lower()):
                if w not in STOP_WORDS and len(w) > 2:
                    word_views[w].append(v["view_count"])

    gap_rows = [
        {"keyword": w, "frequency": len(vl), "avg_views_when_present": int(sum(vl) / len(vl))}
        for w, vl in word_views.items()
        if w not in my_words and len(vl) >= 2
    ]
    gap_rows.sort(key=lambda x: (-x["frequency"], -x["avg_views_when_present"]))

    print(f"\n  Keyword Gap Finder:")
    print(f"  Your {len(MY_TITLES)} titles use {len(my_words)} unique content words.")
    print(f"  High-performing words you're NOT using (frequency ≥ 2 in top videos):\n")
    for row in gap_rows[:20]:
        print(f"    {row['keyword']:<20} {row['frequency']:>2}x in top videos  "
              f"avg {row['avg_views_when_present']:>10,} views")

    return gap_rows


def score_title(title, master):
    """Score a title out of 10 based on patterns from top-performing videos."""
    # Build power words from top quartile of master (or empty if no data)
    power_words: set = set()
    if master:
        sorted_vids = sorted(master.values(), key=lambda v: _safe_int(v.get("view_count", 0)), reverse=True)
        top_q = sorted_vids[:max(1, len(sorted_vids) // 4)]
        word_counter: Counter = Counter()
        for v in top_q:
            for w in re.findall(r"\b[a-zA-Z]+\b", str(v.get("title", "")).lower()):
                if w not in STOP_WORDS and len(w) > 2:
                    word_counter[w] += 1
        power_words = {w for w, _ in word_counter.most_common(30)}

    score = 0
    breakdown = []

    # 1. Length (0–2 pts)
    length = len(title)
    if 45 <= length <= 70:
        pts = 2
    elif 35 <= length <= 85:
        pts = 1
    else:
        pts = 0
    score += pts
    breakdown.append(f"  Length {length} chars {'(optimal 45-70)' if pts == 2 else '(acceptable 35-85)' if pts == 1 else '(outside optimal range)':<28} +{pts}")

    # 2. Power keywords (0–3 pts)
    title_words = {w for w in re.findall(r"\b[a-zA-Z]+\b", title.lower()) if len(w) > 2}
    matches = title_words & power_words
    pts = min(3, len(matches))
    score += pts
    sample = ", ".join(list(matches)[:5]) if matches else "none"
    breakdown.append(f"  Power keywords ({sample}): +{pts}")

    # 3. Sentiment strength (0–2 pts)
    sentiment = classify_sentiment(title)
    pts = SENTIMENT_STRENGTH.get(sentiment, 0)
    score += pts
    breakdown.append(f"  Sentiment ({sentiment}): +{pts}")

    # 4. Format signals: brackets/parens + year (0–2 pts)
    pts = 0
    if re.search(r"[\[\(]", title):
        pts += 1
    if re.search(r"\b(202[4-9]|203\d)\b", title):
        pts += 1
    score += pts
    breakdown.append(f"  Format signals (brackets/year): +{pts}")

    # 5. Number present (0–1 pt)
    pts = 1 if re.search(r"\d", title) else 0
    score += pts
    breakdown.append(f"  Contains a number: +{pts}")

    return score, sentiment, breakdown


# ===========================================================================
# Option 7: My channel stats
# ===========================================================================

def my_channel_stats(yt):
    """Fetch and analyse your own channel's performance."""
    if not MY_CHANNEL_ID:
        print("\n  MY_CHANNEL_ID not set in .env — add it and try again.")
        print("  Example: MY_CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxx")
        return

    print(f"\n  Fetching channel overview ...")
    try:
        resp = yt.channels().list(
            part="snippet,statistics,contentDetails",
            id=MY_CHANNEL_ID,
        ).execute()
    except HttpError as exc:
        print(f"  API error: {exc}")
        return

    items = resp.get("items", [])
    if not items:
        print("  Channel not found. Double-check MY_CHANNEL_ID in .env")
        return

    ch = items[0]
    ch_name = ch["snippet"].get("title", "Unknown")
    stats = ch.get("statistics", {})
    subscriber_count = _safe_int(stats.get("subscriberCount"))
    total_views = _safe_int(stats.get("viewCount"))
    total_video_count = _safe_int(stats.get("videoCount"))
    uploads_playlist = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")

    print(f"  Channel : {ch_name}")
    print(f"  Subs    : {subscriber_count:,}  |  Total views: {total_views:,}  |  Videos: {total_video_count:,}")

    if not uploads_playlist:
        print("  Could not find uploads playlist. Cannot fetch videos.")
        return

    # Fetch all video IDs from the uploads playlist (paginate)
    print(f"\n  Fetching all uploaded video IDs ...", end=" ", flush=True)
    video_ids = []
    token = None
    while True:
        try:
            pl_resp = yt.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist,
                maxResults=50,
                pageToken=token,
            ).execute()
        except HttpError as exc:
            print(f"\n  API error fetching playlist: {exc}")
            break
        for item in pl_resp.get("items", []):
            vid_id = item.get("contentDetails", {}).get("videoId")
            if vid_id:
                video_ids.append(vid_id)
        token = pl_resp.get("nextPageToken")
        if not token:
            break
    print(f"{len(video_ids)} found")

    if not video_ids:
        print("  No videos returned.")
        return

    print(f"  Fetching full metadata for {len(video_ids)} videos ...")
    details = fetch_video_details(yt, video_ids)
    videos = list(details.values())

    # Attach video age and sentiment (reuse existing helpers)
    today = datetime.now(timezone.utc).date()
    for v in videos:
        upload_date = v.get("upload_date", "")
        if upload_date:
            try:
                v["video_age_days"] = (today - datetime.strptime(upload_date, "%Y-%m-%d").date()).days
            except ValueError:
                v["video_age_days"] = None
        else:
            v["video_age_days"] = None
        v["sentiment"] = classify_sentiment(v["title"])

    # Stats
    view_counts = sorted([v["view_count"] for v in videos], reverse=True)
    n = len(view_counts)
    avg_views = int(sum(view_counts) / n) if n else 0
    if n == 0:
        median_views = 0
    elif n % 2 == 1:
        median_views = view_counts[n // 2]
    else:
        median_views = int((view_counts[n // 2 - 1] + view_counts[n // 2]) / 2)

    best = max(videos, key=lambda v: v["view_count"])
    worst = min(videos, key=lambda v: v["view_count"])

    # Upload frequency
    upload_dates = sorted(
        [datetime.strptime(v["upload_date"], "%Y-%m-%d")
         for v in videos if v.get("upload_date")],
        reverse=True,
    )
    if len(upload_dates) >= 2:
        gaps = [(upload_dates[i] - upload_dates[i + 1]).days for i in range(len(upload_dates) - 1)]
        avg_upload_gap = f"{round(sum(gaps) / len(gaps), 1)} days"
    else:
        avg_upload_gap = "N/A"

    # Benchmark against latest competitor research CSV
    competitor_avg = None
    benchmark_source = None
    csvs = sorted(Path(".").glob("youtube_research_*.csv"))
    if csvs:
        try:
            comp_views = pd.to_numeric(
                pd.read_csv(csvs[-1]).get("view_count", pd.Series()), errors="coerce"
            ).dropna()
            if len(comp_views):
                competitor_avg = int(comp_views.mean())
                benchmark_source = csvs[-1].name
        except Exception:
            pass

    # Print summary
    sep = "=" * 72
    thin = "─" * 72
    print(f"\n{sep}")
    print(f"  MY CHANNEL STATS — {ch_name}")
    print(sep)

    print(f"\n  Channel overview:")
    print(f"    Subscribers          {subscriber_count:>12,}")
    print(f"    Total channel views  {total_views:>12,}")
    print(f"    Total videos         {total_video_count:>12,}")

    print(f"\n  Video performance ({n} videos analysed):")
    print(f"    Average views        {avg_views:>12,}")
    print(f"    Median views         {median_views:>12,}")
    print(f"    Avg upload gap       {avg_upload_gap:>12}")
    print(f"    Best video           {best['view_count']:>12,} views — {best['title'][:45]}")
    print(f"                         uploaded {best['upload_date']} | {best['duration']}")
    print(f"    Worst video          {worst['view_count']:>12,} views — {worst['title'][:45]}")
    print(f"                         uploaded {worst['upload_date']} | {worst['duration']}")

    if competitor_avg is not None:
        diff = avg_views - competitor_avg
        sign = "+" if diff >= 0 else ""
        diff_pct = 100 * diff / max(competitor_avg, 1)
        print(f"\n  Benchmark vs small-channel competitors ({benchmark_source}):")
        print(f"    My avg views         {avg_views:>12,}")
        print(f"    Competitor avg views {competitor_avg:>12,}")
        print(f"    Difference           {sign}{diff:>+11,}  ({sign}{diff_pct:.1f}%)")

    print(f"\n{thin}")
    print(f"  All videos by views:")
    print(f"  {'Views':>10}  {'Age(d)':>6}  {'Date':>10}  Title")
    print(f"  {'─'*10}  {'─'*6}  {'─'*10}  {'─'*44}")
    for v in sorted(videos, key=lambda x: x["view_count"], reverse=True):
        age = str(v.get("video_age_days", "?"))
        print(f"  {v['view_count']:>10,}  {age:>6}  {v['upload_date']:>10}  {v['title'][:55]}")

    print(f"\n{sep}\n")

    # Export CSV
    export_cols = [
        "title", "view_count", "like_count", "comment_count",
        "upload_date", "video_age_days", "duration", "sentiment",
        "upload_day_of_week", "upload_hour_utc", "thumbnail_url", "video_id",
    ]
    rows = [{col: v.get(col, "") for col in export_cols} for v in videos]
    df = pd.DataFrame(rows, columns=export_cols)
    df.sort_values("view_count", ascending=False, inplace=True)
    fname = f"youtube_my_channel_{_ts()}.csv"
    df.to_csv(fname, index=False)
    print(f"  CSV saved → {fname}  ({len(df)} videos)\n")


# ===========================================================================
# Option 8: YouTube Analytics (OAuth 2.0)
# ===========================================================================

TRAFFIC_SOURCE_LABELS = {
    "YT_SEARCH": "YouTube Search",
    "SUGGESTED_VIDEO": "Suggested Videos",
    "BROWSE_FEATURES": "Browse / Homepage",
    "EXTERNAL": "External / Google",
    "CHANNEL": "Channel Page",
    "DIRECT_OR_UNKNOWN": "Direct / Unknown",
    "NOTIFICATION": "Notifications",
    "PLAYLIST": "Playlist",
    "SHORTS": "Shorts Feed",
    "END_SCREEN": "End Screen",
    "SUBSCRIBER": "Subscriber Feed",
}


def get_analytics_credentials():
    """Return valid OAuth credentials, running browser flow if needed."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, ANALYTICS_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            if not CLIENT_SECRET_FILE.exists():
                print("\n  client_secret.json not found.")
                print("  To set it up:")
                print("    1. Go to console.cloud.google.com")
                print("    2. APIs & Services → Library → enable 'YouTube Analytics API'")
                print("    3. APIs & Services → Credentials → Create Credentials → OAuth client ID")
                print("    4. Application type: Desktop app")
                print("    5. Download the JSON → save as client_secret.json in this folder")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_FILE), ANALYTICS_SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return creds


def _analytics_query(analytics, metrics, dimensions=None, filters=None,
                     start_date="2020-01-01", sort=None, max_results=200):
    """Thin wrapper around analytics.reports().query() with shared params."""
    today = datetime.now().strftime("%Y-%m-%d")
    kwargs = dict(
        ids="channel==MINE",
        startDate=start_date,
        endDate=today,
        metrics=metrics,
        maxResults=max_results,
    )
    if dimensions:
        kwargs["dimensions"] = dimensions
    if filters:
        kwargs["filters"] = filters
    if sort:
        kwargs["sort"] = sort
    try:
        return analytics.reports().query(**kwargs).execute()
    except HttpError as exc:
        print(f"  Analytics API error: {exc}")
        return None


def _parse_analytics_rows(resp):
    """Turn a reports.query() response into a list of dicts."""
    if not resp or not resp.get("rows"):
        return []
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in resp["rows"]]


def my_channel_deep_analytics(yt):
    """Full YouTube Analytics deep dive using OAuth."""

    # ── OAuth ─────────────────────────────────────────────────────────────
    print("\n  Authenticating with YouTube Analytics ...")
    creds = get_analytics_credentials()
    if not creds:
        return
    analytics = build("youtubeAnalytics", "v2", credentials=creds)
    print("  Authenticated.")

    # ── Pull video metadata via Data API (titles, dates, durations) ───────
    if not MY_CHANNEL_ID:
        print("  MY_CHANNEL_ID not set in .env — add it and try again.")
        return

    try:
        ch_resp = yt.channels().list(
            part="snippet,contentDetails", id=MY_CHANNEL_ID
        ).execute()
    except HttpError as exc:
        print(f"  Data API error: {exc}")
        return

    if not ch_resp.get("items"):
        print("  Channel not found. Check MY_CHANNEL_ID in .env")
        return

    ch_item = ch_resp["items"][0]
    ch_name = ch_item["snippet"]["title"]
    uploads_playlist = ch_item["contentDetails"]["relatedPlaylists"]["uploads"]

    # Paginate through uploads playlist
    print(f"  Fetching video list for {ch_name} ...", end=" ", flush=True)
    video_ids = []
    token = None
    while True:
        try:
            pl = yt.playlistItems().list(
                part="contentDetails", playlistId=uploads_playlist,
                maxResults=50, pageToken=token,
            ).execute()
        except HttpError as exc:
            print(f"\n  Playlist fetch error: {exc}")
            break
        for item in pl.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        token = pl.get("nextPageToken")
        if not token:
            break
    print(f"{len(video_ids)} videos")

    # Fetch full metadata for all videos
    print("  Fetching video metadata ...", end=" ", flush=True)
    details = fetch_video_details(yt, video_ids)
    print("done")

    today_d = datetime.now(timezone.utc).date()
    meta = {}
    for v in details.values():
        upload_date = v.get("upload_date", "")
        age = None
        if upload_date:
            try:
                age = (today_d - datetime.strptime(upload_date, "%Y-%m-%d").date()).days
            except ValueError:
                pass
        meta[v["video_id"]] = {
            "title": v["title"],
            "upload_date": upload_date,
            "upload_day_of_week": v.get("upload_day_of_week", ""),
            "duration_seconds": v.get("duration_seconds", 0),
            "duration": v.get("duration", ""),
            "video_age_days": age,
            "is_short": v.get("duration_seconds", 0) <= 60,
            "thumbnail_url": v.get("thumbnail_url", ""),
        }

    # ── Analytics API calls ────────────────────────────────────────────────
    start_date = "2020-01-01"

    print("  Fetching per-video analytics ...", end=" ", flush=True)
    video_resp = _analytics_query(
        analytics,
        metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,subscribersGained",
        dimensions="video",
        sort="-views",
        start_date=start_date,
    )
    print("done")

    print("  Fetching traffic sources ...", end=" ", flush=True)
    traffic_resp = _analytics_query(
        analytics,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        sort="-views",
        start_date=start_date,
    )
    print("done")

    # ── Build per-video rows ───────────────────────────────────────────────
    video_rows = []
    for row in _parse_analytics_rows(video_resp):
        vid_id = row.get("video", "")
        m = meta.get(vid_id, {})
        video_rows.append({
            "video_id": vid_id,
            "title": m.get("title", f"[{vid_id}]"),
            "upload_date": m.get("upload_date", ""),
            "upload_day_of_week": m.get("upload_day_of_week", ""),
            "video_age_days": m.get("video_age_days"),
            "duration": m.get("duration", ""),
            "duration_seconds": m.get("duration_seconds", 0),
            "is_short": m.get("is_short", False),
            "views": _safe_int(row.get("views", 0)),
            "watch_time_minutes": round(float(row.get("estimatedMinutesWatched", 0) or 0), 1),
            "avg_view_duration_sec": round(float(row.get("averageViewDuration", 0) or 0), 1),
            "avg_view_pct": round(float(row.get("averageViewPercentage", 0) or 0), 1),
            "likes": _safe_int(row.get("likes", 0)),
            "subscribers_gained": _safe_int(row.get("subscribersGained", 0)),
        })

    # ── Traffic source rows ────────────────────────────────────────────────
    traffic_rows = []
    raw_traffic = _parse_analytics_rows(traffic_resp)
    if raw_traffic:
        total_tv = sum(_safe_int(r.get("views", 0)) for r in raw_traffic)
        for row in raw_traffic:
            src = row.get("insightTrafficSourceType", "UNKNOWN")
            v = _safe_int(row.get("views", 0))
            traffic_rows.append({
                "source": TRAFFIC_SOURCE_LABELS.get(src, src),
                "views": v,
                "pct_of_views": round(100 * v / max(total_tv, 1), 1),
                "watch_time_minutes": round(float(row.get("estimatedMinutesWatched", 0) or 0), 1),
            })

    if not video_rows:
        print("\n  No analytics data returned — channel may be too new or have no views yet.")
        return

    # ── Aggregates ────────────────────────────────────────────────────────
    def _avg(lst, key):
        vals = [v[key] for v in lst if v.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0

    shorts = [v for v in video_rows if v["is_short"]]
    longform = [v for v in video_rows if not v["is_short"]]

    by_views = sorted(video_rows, key=lambda v: v["views"], reverse=True)
    by_duration = sorted(video_rows, key=lambda v: v["avg_view_duration_sec"], reverse=True)
    by_retention = sorted(video_rows, key=lambda v: v["avg_view_pct"], reverse=True)

    DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_views: dict = defaultdict(list)
    for v in video_rows:
        if v.get("upload_day_of_week"):
            day_views[v["upload_day_of_week"]].append(v["views"])

    # ── Terminal output ────────────────────────────────────────────────────
    sep = "=" * 72
    thin = "─" * 72
    print(f"\n{sep}")
    print(f"  DEEP ANALYTICS — {ch_name}")
    print(sep)

    total_views = sum(v["views"] for v in video_rows)
    total_watch = sum(v["watch_time_minutes"] for v in video_rows)
    total_subs = sum(v["subscribers_gained"] for v in video_rows)

    print(f"\n  Overview ({len(video_rows)} videos with data):")
    print(f"    Total views           {total_views:>12,}")
    print(f"    Total watch time      {total_watch:>12,.0f} min  ({total_watch/60:.1f} hrs)")
    print(f"    Subscribers gained    {total_subs:>12,}")
    print(f"    Avg views / video     {_avg(video_rows, 'views'):>12,.0f}")
    print(f"    Avg watch time/video  {_avg(video_rows, 'watch_time_minutes'):>12,.1f} min")
    print(f"    Avg view duration     {_avg(video_rows, 'avg_view_duration_sec'):>12,.0f} sec")
    print(f"    Avg view retention    {_avg(video_rows, 'avg_view_pct'):>12,.1f}%")

    if longform or shorts:
        print(f"\n{thin}")
        print(f"  Shorts vs Long-form:")
        print(f"  {'Format':<12} {'Count':>5}  {'Avg Views':>10}  {'Avg Duration':>12}  {'Avg Retention':>13}")
        if longform:
            print(f"  {'Long-form':<12} {len(longform):>5}  "
                  f"{_avg(longform, 'views'):>10,.0f}  "
                  f"{_avg(longform, 'avg_view_duration_sec'):>10,.0f}s  "
                  f"{_avg(longform, 'avg_view_pct'):>12,.1f}%")
        if shorts:
            print(f"  {'Shorts':<12} {len(shorts):>5}  "
                  f"{_avg(shorts, 'views'):>10,.0f}  "
                  f"{_avg(shorts, 'avg_view_duration_sec'):>10,.0f}s  "
                  f"{_avg(shorts, 'avg_view_pct'):>12,.1f}%")

    print(f"\n{thin}")
    print("  Top 5 by views:")
    for i, v in enumerate(by_views[:5], 1):
        print(f"    {i}. {v['views']:>7,} views  {v['avg_view_pct']:>5.1f}% retention  {v['title'][:50]}")

    print(f"\n  Top 5 by avg view duration:")
    for i, v in enumerate(by_duration[:5], 1):
        m, s = divmod(int(v["avg_view_duration_sec"]), 60)
        print(f"    {i}. {m}m{s:02d}s avg  {v['views']:>7,} views  {v['title'][:48]}")

    print(f"\n  Top 5 by avg retention %:")
    for i, v in enumerate(by_retention[:5], 1):
        print(f"    {i}. {v['avg_view_pct']:>5.1f}% retained  {v['views']:>7,} views  {v['title'][:48]}")

    if traffic_rows:
        print(f"\n{thin}")
        print("  Traffic Sources:")
        max_pct = max(r["pct_of_views"] for r in traffic_rows)
        for r in sorted(traffic_rows, key=lambda x: -x["views"]):
            bar = "█" * int(20 * r["pct_of_views"] / max(max_pct, 1))
            print(f"    {r['source']:<26} {bar:<20} {r['pct_of_views']:>5.1f}%  ({r['views']:,} views)")

    if day_views:
        print(f"\n{thin}")
        print("  Upload Day Impact (avg views):")
        day_avgs = [(d, day_views[d]) for d in DAY_ORDER if d in day_views]
        max_avg_day = max(sum(v) / len(v) for _, v in day_avgs) if day_avgs else 1
        for day, vl in sorted(day_avgs, key=lambda x: -(sum(x[1]) / len(x[1]))):
            avg_v = sum(vl) / len(vl)
            bar = "█" * int(20 * avg_v / max(max_avg_day, 1))
            print(f"    {day:<10} {bar:<20} {avg_v:>8,.0f} avg views  ({len(vl)} videos)")

    print(f"\n{sep}\n")

    # ── Export CSVs ────────────────────────────────────────────────────────
    vid_cols = [
        "title", "views", "watch_time_minutes", "avg_view_duration_sec",
        "avg_view_pct", "likes", "subscribers_gained",
        "upload_date", "upload_day_of_week", "video_age_days",
        "duration", "is_short", "video_id",
    ]
    df_vid = pd.DataFrame(
        [{c: v.get(c, "") for c in vid_cols} for v in video_rows],
        columns=vid_cols,
    ).sort_values("views", ascending=False)
    fname1 = f"youtube_deep_analytics_{_ts()}.csv"
    df_vid.to_csv(fname1, index=False)
    print(f"  Per-video analytics  → {fname1}")

    if traffic_rows:
        fname2 = f"youtube_traffic_sources_{_ts()}.csv"
        pd.DataFrame(traffic_rows).to_csv(fname2, index=False)
        print(f"  Traffic sources      → {fname2}")

    print()


# ===========================================================================
# Trending tracker
# ===========================================================================

def trending_save(master, timestamp):
    DATA_DIR.mkdir(exist_ok=True)
    snapshot = {
        "timestamp": timestamp,
        "videos": [
            {
                "video_id": v["video_id"],
                "title": v["title"],
                "channel_name": v["channel_name"],
                "view_count": v.get("view_count", 0),
                "keywords": v.get("keywords", ""),
                "upload_date": v.get("upload_date", ""),
            }
            for v in master.values()
        ],
    }
    path = DATA_DIR / f"run_{timestamp.replace(':', '-').replace(' ', '_')}.json"
    path.write_text(json.dumps(snapshot, indent=2))
    print(f"  Run snapshot saved → {path}")


def trending_compare(master):
    """Load previous run and flag new videos and fast growers."""
    runs = sorted(DATA_DIR.glob("run_*.json"))
    if len(runs) < 2:
        print("\n  Trending tracker: need at least 2 runs to compare. Run again tomorrow!")
        return []

    prev = json.loads(runs[-2].read_text())
    print(f"\n  Comparing against run from: {prev['timestamp']}")

    prev_views = {v["video_id"]: v["view_count"] for v in prev["videos"]}
    prev_ids = set(prev_views.keys())

    new_entries, growers = [], []
    for video in master.values():
        vid_id = video["video_id"]
        views = _safe_int(video.get("view_count", 0))
        if vid_id not in prev_ids:
            new_entries.append({
                "status": "NEW",
                "title": video["title"],
                "channel_name": video["channel_name"],
                "view_count": views,
                "keywords": video.get("keywords", ""),
            })
        else:
            old = prev_views[vid_id]
            pct = 100 * (views - old) / max(old, 1)
            if pct >= COMPETITOR_VIEW_GROWTH_PCT:
                growers.append({
                    "status": "GROWING",
                    "title": video["title"],
                    "channel_name": video["channel_name"],
                    "view_count": views,
                    "prev_view_count": old,
                    "pct_change": round(pct, 1),
                    "keywords": video.get("keywords", ""),
                })

    if new_entries:
        print(f"\n  NEW videos ({len(new_entries)}):")
        for v in new_entries:
            print(f"    + {v['view_count']:>10,} views — {v['title']}")
            print(f"      {v['channel_name']}  |  keywords: {v['keywords']}")
    else:
        print("\n  No new videos since last run.")

    if growers:
        print(f"\n  FAST-GROWING videos (≥{COMPETITOR_VIEW_GROWTH_PCT}% view increase):")
        for v in sorted(growers, key=lambda x: -x["pct_change"]):
            print(f"    ↑{v['pct_change']:+.0f}%  {v['view_count']:>10,} views — {v['title']}")
    else:
        print("  No fast-growing videos detected.")

    return new_entries + growers


# ===========================================================================
# Analysis helpers (existing)
# ===========================================================================

def analyze_titles(titles):
    word_counts: Counter = Counter()
    starts_number = questions = brackets = 0
    for title in titles:
        for w in re.findall(r"\b[a-zA-Z]+\b", title.lower()):
            if w not in STOP_WORDS and len(w) > 2:
                word_counts[w] += 1
        if re.match(r"^\d", title):
            starts_number += 1
        if "?" in title or title.lower().startswith("how"):
            questions += 1
        if "[" in title or "(" in title:
            brackets += 1
    n = len(titles) or 1
    return {
        "top_words": word_counts.most_common(15),
        "avg_title_length": sum(len(t) for t in titles) / n,
        "pct_starts_with_number": 100 * starts_number / n,
        "pct_questions": 100 * questions / n,
        "pct_brackets": 100 * brackets / n,
    }


# ===========================================================================
# Export helpers
# ===========================================================================

def _ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def export_df(rows, suffix):
    if not rows:
        return None
    fname = f"youtube_{suffix}_{_ts()}.csv"
    pd.DataFrame(rows).to_csv(fname, index=False)
    return fname


def export_main_csv(master):
    columns = [
        "keywords", "title", "channel_name", "subscriber_count",
        "channel_keyword_count", "is_competitor",
        "view_count", "like_count", "comment_count",
        "video_age_days", "is_recent", "sentiment",
        "upload_date", "upload_day_of_week", "upload_hour_utc",
        "duration", "thumbnail_url", "video_id",
    ]
    rows = [{col: v.get(col, "") for col in columns} for v in master.values()]
    df = pd.DataFrame(rows, columns=columns)
    df.sort_values(["channel_keyword_count", "view_count"], ascending=[False, False], inplace=True)
    fname = f"youtube_research_{_ts()}.csv"
    df.to_csv(fname, index=False)
    return fname


# ===========================================================================
# Terminal summary
# ===========================================================================

def print_summary(all_results, master):
    sep = "=" * 72
    thin = "─" * 72
    print(f"\n{sep}")
    print("  YOUTUBE RESEARCH SUMMARY")
    print(sep)

    for keyword, videos in all_results.items():
        print(f"\n{thin}")
        print(f'  Keyword: "{keyword}"')
        if not videos:
            print("  No results after filtering.")
            continue

        by_views = sorted(videos, key=lambda v: v["view_count"], reverse=True)
        avg_views = sum(v["view_count"] for v in videos) / len(videos)
        recent = sum(1 for v in videos if master.get(v["video_id"], {}).get("is_recent"))
        print(f"  Videos: {len(videos)}  |  Avg views: {avg_views:,.0f}  |  Recent (≤1yr): {recent}")

        print("\n  Top 5 performing videos:")
        for rank, v in enumerate(by_views[:5], 1):
            mv = master.get(v["video_id"], {})
            flags = []
            if mv.get("channel_keyword_count", 1) > 1:
                flags.append("★ COMPETITOR")
            if mv.get("is_recent"):
                flags.append("RECENT")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            sentiment = mv.get("sentiment", classify_sentiment(v["title"]))
            print(f"    {rank}. {v['view_count']:>10,} views  [{sentiment}]{flag_str}")
            print(f"         {v['title']}")
            print(f"         {v['channel_name']} | {v['upload_date']} | {v['duration']}")

        patterns = analyze_titles([v["title"] for v in videos])
        top_words = ", ".join(w for w, _ in patterns["top_words"][:10])
        print("\n  Title patterns:")
        print(f"    Avg length:         {patterns['avg_title_length']:.0f} chars")
        print(f"    Start with number:  {patterns['pct_starts_with_number']:.0f}%")
        print(f"    Questions:          {patterns['pct_questions']:.0f}%")
        print(f"    Brackets/parens:    {patterns['pct_brackets']:.0f}%")
        print(f"    Top words:          {top_words}")

    # Repeat competitors
    ch_kws: dict = {}
    for keyword, videos in all_results.items():
        for v in videos:
            ch_kws.setdefault(v["channel_name"], set()).add(keyword)
    competitors = {ch: kws for ch, kws in ch_kws.items() if len(kws) > 1}
    if competitors:
        print(f"\n{thin}")
        print("  REPEAT COMPETITORS  (ranking in 2+ of your keywords — study these)")
        for ch, kws in sorted(competitors.items(), key=lambda x: -len(x[1])):
            print(f"    • {ch}  →  {len(kws)} keywords: {', '.join(sorted(kws))}")

    print(f"\n{sep}\n")


# ===========================================================================
# Menu & main
# ===========================================================================

def show_menu():
    print("\n" + "=" * 54)
    print("  YouTube Research Tool")
    print("=" * 54)
    print("  1.  Full research (all features)")
    print("  2.  Competitor analysis only")
    print("  3.  Title & keyword analysis only")
    print("  4.  Score a title")
    print("  5.  Check for trending changes")
    print("  6.  Download thumbnails from last research")
    print("  7.  My channel stats")
    print("  8.  My channel deep analytics (OAuth)")
    print("=" * 54)
    return input("  Select [1-8]: ").strip()


def main():
    DATA_DIR.mkdir(exist_ok=True)
    THUMBNAILS_DIR.mkdir(exist_ok=True)

    choice = show_menu()

    # ── Option 4: score a title (no API needed) ────────────────────────────
    if choice == "4":
        title = input("\n  Enter title to score: ").strip()
        if not title:
            print("  No title entered.")
            return
        master = _load_master_from_latest_csv()
        if not master:
            print("  (No prior research CSV found — scoring based on format signals only)")
        score, sentiment, breakdown = score_title(title, master)
        print(f"\n  Title:     {title}")
        print(f"  Sentiment: {sentiment}")
        print(f"  Score:     {score}/10\n")
        for line in breakdown:
            print(line)
        print()
        return

    # ── Option 5: trending diff (no API needed) ────────────────────────────
    if choice == "5":
        runs = sorted(DATA_DIR.glob("run_*.json"))
        if not runs:
            print("\n  No run snapshots found. Run a full research first.")
            return
        latest = json.loads(runs[-1].read_text())
        master = {v["video_id"]: v for v in latest["videos"]}
        changes = trending_compare(master)
        if changes:
            fname = export_df(changes, "trending_changes")
            if fname:
                print(f"\n  Changes saved → {fname}")
        return

    # ── Option 6: download thumbnails from a CSV (no API needed) ──────────
    if choice == "6":
        csvs = sorted(Path(".").glob("youtube_research_*.csv"))
        if not csvs:
            print("\n  No research CSV found. Run a full research first (option 1).")
            return
        # Default to the most recent CSV, but let the user override
        default = csvs[-1]
        print(f"\n  Most recent CSV: {default.name}")
        user_input = input(f"  Press Enter to use it, or type a filename: ").strip()
        target = Path(user_input) if user_input else default
        download_thumbnails_from_csv(target)
        return

    # ── Option 7: my channel stats ────────────────────────────────────────
    if choice == "7":
        yt = youtube_client()
        my_channel_stats(yt)
        return

    # ── Option 8: deep analytics via OAuth ────────────────────────────────
    if choice == "8":
        yt = youtube_client()
        my_channel_deep_analytics(yt)
        return

    # ── Options 1-3: need the YouTube API ─────────────────────────────────
    yt = youtube_client()
    print(f"\n  Config: {len(KEYWORDS)} keywords | {MAX_RESULTS}/keyword | "
          f"≤{SUBSCRIBER_LIMIT:,} subs | min {MIN_DURATION_SECONDS // 60}m\n")

    all_results, master = run_research(yt)

    if not master:
        print("\n  No results found after filtering.")
        return

    # Save snapshot for trending tracker
    trending_save(master, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    do_competitor = choice in ("1", "2")
    do_titles = choice in ("1", "3")

    # ── Competitor analysis ────────────────────────────────────────────────
    if do_competitor:
        print("\n--- Competitor Analysis ---")

        download_thumbnails(all_results)

        dive_rows = channel_deep_dive(yt, master)
        if dive_rows:
            fname = export_df(dive_rows, "channel_deep_dive")
            if fname:
                print(f"  Channel deep dive → {fname}")

        day_rows, hour_rows = upload_timing_analysis(master)
        fname = export_df(day_rows, "timing_by_day")
        if fname:
            print(f"  Timing by day     → {fname}")
        fname = export_df(hour_rows, "timing_by_hour")
        if fname:
            print(f"  Timing by hour    → {fname}")

    # ── Title & keyword analysis ───────────────────────────────────────────
    if do_titles:
        print("\n--- Title & Keyword Analysis ---")

        sentiment_rows = sentiment_analysis(all_results)
        fname = export_df(sentiment_rows, "sentiment_analysis")
        if fname:
            print(f"  Sentiment analysis → {fname}")

        gap_rows = keyword_gap_finder(master, all_results)
        fname = export_df(gap_rows, "keyword_gaps")
        if fname:
            print(f"  Keyword gaps       → {fname}")

        # Include timing if not already done in competitor block
        if not do_competitor:
            day_rows, hour_rows = upload_timing_analysis(master)
            fname = export_df(day_rows, "timing_by_day")
            if fname:
                print(f"  Timing by day      → {fname}")
            fname = export_df(hour_rows, "timing_by_hour")
            if fname:
                print(f"  Timing by hour     → {fname}")

    # ── Trending comparison ────────────────────────────────────────────────
    changes = trending_compare(master)
    if changes:
        fname = export_df(changes, "trending_changes")
        if fname:
            print(f"\n  Trending changes   → {fname}")

    # ── Summary + main CSV ────────────────────────────────────────────────
    print_summary(all_results, master)
    fname = export_main_csv(master)
    if fname:
        unique = len(master)
        total = sum(len(v) for v in all_results.values())
        print(f"Main CSV → {fname}  ({unique} unique videos, {total - unique} duplicates removed)\n")


# ===========================================================================
# Utilities
# ===========================================================================

def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_duration(iso_str):
    try:
        total = int(isodate.parse_duration(iso_str).total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return total, f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return 0, "N/A"


def _load_master_from_latest_csv():
    csvs = sorted(Path(".").glob("youtube_research_*.csv"))
    if not csvs:
        return {}
    try:
        df = pd.read_csv(csvs[-1])
        return {str(row.get("video_id", i)): row.to_dict() for i, row in df.iterrows()}
    except Exception:
        return {}


if __name__ == "__main__":
    main()
