def youtube_info(source_url: str) -> tuple[str, list[dict[str, Any]]]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        # ইউটিউব ডেটাসেন্টার IP ব্লক বাইপাস করার মূল অপশন
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'android', 'ios'],
                'skip': ['webpage', 'authcheck'],
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(source_url, download=False)
        except Exception as error:
            raise RuntimeError(f"YouTube URL প্রসেস করা যায়নি: {error}")

    duration = info.get("duration", 0)
    if duration > MAX_VIDEO_SECONDS:
        raise RuntimeError(f"ভিডিওর দৈর্ঘ্য {duration // 60} মিনিট। সর্বোচ্চ সীমা {MAX_VIDEO_SECONDS // 60} মিনিট।")

    title = info.get("title", "YouTube Video")
    formats = info.get("formats", [])
    
    options: list[dict[str, Any]] = []
    video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("url")]
    audio_formats = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
    
    audio_url = audio_formats[-1].get("url") if audio_formats else ""
    sorted_vids = sorted(video_formats, key=lambda x: x.get("height") or 0, reverse=True)
    seen_heights = set()

    for fmt in sorted_vids:
        height = fmt.get("height", 0)
        if height and height not in seen_heights and height <= 1080:
            seen_heights.add(height)
            options.append({
                "format": fmt.get("format_id"),
                "url": fmt.get("url"),
                "audio_url": audio_url or fmt.get("url"),
                "height": height,
                "size": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                "ext": "mp4",
                "label": f"{height}p HD" if height >= 720 else f"{height}p SD",
            })
            if len(options) >= 4:
                break

    if not options and sorted_vids:
        fmt = sorted_vids[0]
        options.append({
            "format": fmt.get("format_id"),
            "url": fmt.get("url"),
            "audio_url": audio_url or fmt.get("url"),
            "height": fmt.get("height", 0),
            "size": fmt.get("filesize") or 0,
            "ext": "mp4",
            "label": "Auto Quality",
        })

    if not options:
        raise RuntimeError("ডাউনলোডের মতো কোনো ভিডিও ফরম্যাট পাওয়া যায়নি।")

    return title, options
