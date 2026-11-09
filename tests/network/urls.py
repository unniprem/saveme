"""Real-site URLs for the opt-in network checks (`pytest -m network`), kept in one place.

If a site removes a video or rate-limits, swap the URL here and note the swap.
"""

YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
YOUTU_BE = "https://youtu.be/jNQXAC9IVRw"
# yt-dlp's own RedditIE test post (12 s v.redd.it video, r/videos, 2017).
REDDIT = "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/"
# MDN's CC0 sample clip: 5 s H.264 + AAC, ~1.1 MB, served as video/mp4.
DIRECT_MP4 = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
UNSUPPORTED = "https://streamtape.com/"
PRIVATE_IP = "http://192.168.1.1/"
