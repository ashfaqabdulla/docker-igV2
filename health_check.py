"""
Probe the Chrome container's CDP endpoint before a fetch run.
Exit 0 if reachable, 2 if not.
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("IG_CDP_URL", "http://127.0.0.1:9222") + "/json/version"


def main():
    try:
        with urllib.request.urlopen(URL, timeout=3) as r:
            info = json.load(r)
        browser = info.get("Browser", "")
        if not browser:
            print(f"Unexpected CDP response: {info!r}")
            sys.exit(2)
    except Exception as e:
        print(f"Browser not reachable at {URL}: {e}")
        sys.exit(2)
    print(f"Browser reachable: {browser}")


if __name__ == "__main__":
    main()
