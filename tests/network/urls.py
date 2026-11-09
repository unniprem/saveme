"""Real-site URLs for the opt-in network checks (`pytest -m network`), kept in one place.

If a site removes a video or rate-limits, swap the URL here and note the swap.
"""

# Swap: jNQXAC9IVRw ("Me at the zoo") only offers Best (240p), so it cannot show a 480p tier.
# C0DPdy98e4c is an 18 s video offering best + 480 (verified 2026-11-09).
YOUTUBE = "https://www.youtube.com/watch?v=C0DPdy98e4c"
YOUTU_BE = "https://youtu.be/C0DPdy98e4c"
# yt-dlp's own RedditIE test post (12 s v.redd.it video, r/videos, 2017).
REDDIT = "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/"
# MDN's CC0 sample clip: 5 s H.264 + AAC, ~1.1 MB, served as video/mp4.
DIRECT_MP4 = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
# Swap: streamtape.com (and luluvid/doodstream) were unreachable from the test host (502 unreachable),
# so they cannot show `unsupported`. example.com is reachable and has no media.
UNSUPPORTED = "https://example.com/"
PRIVATE_IP = "http://192.168.1.1/"
