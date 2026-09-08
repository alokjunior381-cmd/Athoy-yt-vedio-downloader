def fetch_cobalt_media(source_url: str, is_audio: bool = False) -> str:
    # Cobalt API v10 এনডপয়েন্ট
    api_url = "https://api.cobalt.tools/"
    
    payload_data = {
        "url": source_url,
        "videoQuality": "720"
    }
    
    if is_audio:
        payload_data["downloadMode"] = "audio"
        payload_data["audioFormat"] = "mp3"
        
    payload = json.dumps(payload_data).encode("utf-8")
    
    status, _, raw = http_request(
        api_url,
        method="POST",
        payload=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )
    data = json.loads(raw.decode("utf-8", errors="replace"))
    
    if data.get("status") == "error":
        error_msg = data.get("error", {}).get("code", "ভিডিও প্রসেস করা সম্ভব হয়নি।")
        raise RuntimeError(f"Cobalt Error: {error_msg}")
    
    # Cobalt v10 response
    if data.get("status") in ("tunnel", "redirect", "picker"):
        return data.get("url")
    
    raise RuntimeError("ডাউনলোড লিংক পাওয়া যায়নি।")
