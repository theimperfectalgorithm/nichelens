# NicheLens

**An 8-module YouTube research and analytics tool, built for one niche: AI trading and build-in-public content.**

---

## Why this exists

While documenting the build of my forex trading bot on YouTube, I was posting completely blind. Some videos got 700+ views, others got 14 — and I had no idea why. Tools like TubeBuddy and VidIQ exist, but they're built for general creators, not for a specific niche like algo trading and AI-built systems.

So I built my own — configured for my exact corner of YouTube: AI trading, forex bots, algo trading, Claude AI coding, build in public, MetaTrader 5.

---

## What it does

```
======================================================
  YouTube Research Tool
======================================================
  1.  Full research (all features)
  2.  Competitor analysis only
  3.  Title & keyword analysis only
  4.  Score a title
  5.  Check for trending changes
  6.  Download thumbnails from last research
  7.  My channel stats
  8.  My channel deep analytics (OAuth)
======================================================
```

**Core engine** — searches 7 configurable niche keywords, pulls the top 20 videos per keyword with full metadata, filters out channels over 500k subs and Shorts, deduplicates across keywords, flags repeat competitors.

**Competitor analysis** — thumbnail downloader, channel deep dives (last 10 videos + upload frequency per competitor), upload timing heatmap.

**Title & keyword intelligence** — sentiment classifier (How-To / Results / Curiosity / Fear / Story / Question / General, ranked by performance), keyword gap finder, title scorer (0-10 based on length, power words, sentiment, numbers, brackets), trending tracker (flags 20%+ growth between runs).

**My channel stats** — subscriber count, total views, full video metadata, best/worst performers, benchmarked against small competitor channels.

**Deep analytics (OAuth)** — watch time, average view duration, retention per video, Shorts vs long-form breakdown, traffic sources, upload-day impact. Exports to CSV.

Every run produces timestamped CSVs across all modules, plus organised thumbnail archives.

---

## Real result

Running this against my own channel (2 months old, 28 videos) immediately surfaced a clear pattern:

- "Did You Know" format titles: **~81 views average**
- Story/Outcome format titles: **~340+ views average**
- **~4x performance difference**

That single insight — drop one title format, double down on another — came directly from this tool's sentiment classifier. It's what it was built to do.

---

## Stack

- **Python** — core language
- **Claude Code** — entire system built through it, no prior formal programming background
- **YouTube Data API v3** — search, metadata, public stats
- **OAuth 2.0 + YouTube Analytics API** — owner-only deep analytics

---

## Status

Active CLI tool, running weekly research cycles for [The Imperfect Algorithm](https://www.youtube.com/@TheImPerfectAlgorithm). Build process documented on YouTube.

---

## Where this is going

Currently a personal CLI tool. Next steps: offering it as a done-for-you research service for creators in adjacent niches, eventually a self-serve web dashboard with OAuth channel connection.

---

## Get in touch

Need niche-specific YouTube research, or a custom version for your niche?

→ [zerohand.dev](https://zerohand.dev)
→ hello@zerohand.dev
