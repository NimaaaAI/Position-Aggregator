"""Download every job ad listed in the sitemaps of the sites in sites.yml.

Nothing is filtered. Every ad the site publishes gets saved as-is; sorting out
which ones matter happens later, over the downloaded files.

    python scrape.py --list      read the sitemap, save the URL list, print the count
    python scrape.py --one       download a single ad, to check before the rest
    python scrape.py --all       download everything not already downloaded
    python scrape.py --update    the nightly job: refresh, diff, download what is new
"""

import argparse
import re
import sys
import time
import urllib.parse
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def get(url, tries=4):
    """One request, waiting and retrying when the server asks us to.

    A 429 is the server pacing the client, and Retry-After is it saying how long to
    wait. Honouring that is what the status code is for -- it is the polite response,
    not a way round anything. A 403 or a challenge page is a refusal, and the caller
    stops rather than dressing the request up as something else.
    """
    for attempt in range(tries):
        response = requests.get(url, headers=HEADERS, timeout=45)
        print(f"  {response.status_code}  {len(response.content):>9,}b  {url}")
        if response.status_code != 429:
            return response

        # Retry-After is meant to say how long to wait, but this one answers
        # "0.000" -- retry at once, which is what earned the 429. So it is treated
        # as a floor under a backoff that grows each attempt, never as the whole
        # answer: 60s, then 120s, then 180s.
        try:
            asked = float(response.headers.get("Retry-After") or 0)
        except ValueError:
            asked = 0.0
        wait = max(asked, 60 * (attempt + 1))
        print(f"  rate limited (Retry-After: {response.headers.get('Retry-After')}), "
              f"waiting {wait:.0f}s")
        time.sleep(wait)
    return response


def ad_id(url, pattern):
    """The site's own number for the ad, used as its filename.

    Where that number sits differs by site -- last path segment on one, third on
    another -- so the pattern comes from sites.yml rather than being guessed here.
    A pattern that does not match is a config mistake worth stopping for: falling
    back to the slug would quietly file every ad under the wrong name.
    """
    found = re.search(pattern, url)
    if not found:
        sys.exit(f"id_pattern {pattern!r} does not match {url}")
    return found.group(1)


parser = argparse.ArgumentParser()
parser.add_argument("--list", action="store_true", help="collect the URLs only")
parser.add_argument("--one", action="store_true", help="download a single ad")
parser.add_argument("--all", action="store_true", help="download everything")
parser.add_argument("--update", action="store_true",
                    help="nightly: refresh the sitemap, diff it, download what is new")
parser.add_argument("--site", action="append", metavar="NAME",
                    help="only this board. Repeat for several")
parser.add_argument("--skip", action="append", metavar="NAME",
                    help="every board but this one. Repeat for several")
args = parser.parse_args()

if not (args.list or args.one or args.all or args.update):
    sys.exit("pick one: --list, --one, --all or --update")

sites = yaml.safe_load((ROOT / "sites.yml").read_text(encoding="utf-8"))["sites"]

# --site and --skip pick which boards this run touches. Three of the four publish a
# sitemap and finish in seconds; euraxess has none, so its URLs can only be found by
# walking 830 listing pages, which takes most of an hour whether anything changed or
# not. Being able to leave it out is the difference between updating over coffee and
# updating overnight.
#
# A name that matches no board stops the run. Silently skipping nothing, or silently
# selecting nothing, would look like success and cost an hour to notice.
known = {site["name"] for site in sites}
for name in (args.site or []) + (args.skip or []):
    if name not in known:
        sys.exit(f"no board called {name!r}. sites.yml has: {', '.join(sorted(known))}")

if args.site:
    sites = [site for site in sites if site["name"] in args.site]
if args.skip:
    sites = [site for site in sites if site["name"] not in args.skip]
if not sites:
    sys.exit("--site and --skip between them left no boards to do")

for site in sites:
    name = site["name"]
    delay = site.get("delay", 2)
    data = ROOT / "data"
    url_file = data / f"{name}_urls.txt"
    prev_file = data / f"{name}_urls_prev.txt"
    closed_file = data / f"{name}_closed.txt"
    dead_file = data / f"{name}_dead.txt"
    html_dir = data / "raw" / name

    print(f"\n=== {name}")

    # Left as None when the collection below is skipped, so the saved list is read
    # instead. Set by every path that collects, so a cut-short run keeps what it got.
    urls = None

    # ---- collect the ad URLs -------------------------------------------------
    # --update always re-reads the sitemap. Without that it would work from the
    # list saved on the previous run and never notice a single new ad (Most important part).
    if args.list or args.update or not url_file.exists():
        # Cleared only when a collection is cut short. A truncated list must never be
        # saved: the next run would diff against it and record thousands of perfectly
        # open ads as closed.
        complete = True

        if site.get("listing_url"):
            # Some boards publish no sitemap of their ads, so the ad URLs have to
            # come from the listing pages themselves. Walking stops when a page
            # contributes nothing new, which needs no advertised total and copes
            # with the count changing while we walk.
            #
            # id_pattern does the filtering here rather than job_url_contains,
            # because "/jobs/search" and "/jobs/459098" both contain "/jobs/" and
            # only the second is an advert.
            print(f"walking {site['listing_url']}")
            found = set()
            page = 0
            while True:
                response = get(f"{site['listing_url']}?{site['page_param']}={page}")
                # A refused page is not the end of the listing. Told apart from
                # running out of pages because the two need opposite responses:
                # one means the list is finished, the other that it is truncated.
                if response.status_code != 200:
                    print(f"  refused at page {page}: HTTP {response.status_code}")
                    complete = False
                    break
                links = (urllib.parse.urljoin(site["listing_url"], href)
                         for href in re.findall(r'href="([^"]+)"', response.text))
                fresh = {u for u in links if re.search(site["id_pattern"], u)} - found
                if not fresh:
                    break
                found |= fresh
                page += 1
                print(f"  page {page}: {len(fresh)} new, {len(found)} so far")
                time.sleep(delay)
            urls = sorted(found)
        else:
            print("reading the sitemap index")
            index = get(site["sitemap_index"]).text
            sitemaps = [s for s in re.findall(r"<loc>([^<]+)</loc>", index)
                        if site["sitemap_match"] in s]
            print(f"  {len(sitemaps)} matching sitemap(s)")

            urls = []
            for sitemap in sitemaps:
                time.sleep(delay)
                body = get(sitemap).text
                urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", body)
                         if site["job_url_contains"] in u]

            # jobs.ac.uk writes its <loc> values without a scheme -- "www.jobs.ac.uk/
            # job/DQH648/..." -- which requests rejects outright. Fixed here rather
            # than at download time so the saved URL list, the id, and the ad's stored
            # URL all agree, and so nothing downstream has to know a site did this.
            urls = [u if u.startswith("http") else f"https://{u}" for u in urls]
            urls = sorted(set(urls))

        data.mkdir(parents=True, exist_ok=True)

        if not complete:
            # Download what was collected, but leave the saved list alone: it is the
            # only record of which ads exist, and half a list is worse than an old one.
            print(f"\n  {len(urls)} collected before being cut short -- "
                  f"the saved list is left as it was")
            if not url_file.exists():
                url_file.write_text("\n".join(urls), encoding="utf-8")
            urls = sorted(set(urls) | set(url_file.read_text(encoding="utf-8").split()))

        else:
            # Compare against the previous run before overwriting it. An ad that has
            # dropped out of the sitemap has closed, and that is worth recording:
            # its HTML file stays on disk and would otherwise look open forever.
            previous = (set(url_file.read_text(encoding="utf-8").split())
                        if url_file.exists() else set())
            added = sorted(set(urls) - previous)
            gone = sorted(previous - set(urls))

            if url_file.exists():
                prev_file.write_text(url_file.read_text(encoding="utf-8"), encoding="utf-8")
            url_file.write_text("\n".join(urls), encoding="utf-8")

            print(f"\n  {len(urls)} ads listed")
            if previous:
                print(f"  {len(added)} new, {len(gone)} gone, "
                      f"{len(set(urls) & previous)} unchanged")
            if gone:
                with closed_file.open("a", encoding="utf-8") as fh:
                    for url in gone:
                        fh.write(f"{date.today().isoformat()}\t{url}\n")
                print(f"  closures appended to {closed_file.relative_to(ROOT)}")

    if urls is None:
        urls = url_file.read_text(encoding="utf-8").split()
    if args.list:
        continue

    # ---- download the ad pages ----------------------------------------------
    html_dir.mkdir(parents=True, exist_ok=True)

    # Sitemaps go stale: a few ads are listed but already deleted, and answer 404.
    # Without remembering them they would be retried on every run for ever, and the
    # "to go" count would never reach zero.
    dead = set(dead_file.read_text(encoding="utf-8").split()) if dead_file.exists() else set()

    todo = [u for u in urls
            if u not in dead and not (html_dir / f"{ad_id(u, site['id_pattern'])}.html").exists()]

    # Counted before --one truncates the list, otherwise "already downloaded" is
    # whatever is left over and reads as though the whole board were already on disk.
    print(f"\n{len(urls) - len(todo) - len(dead & set(urls))} already downloaded, "
          f"{len(todo)} to go, {len(dead & set(urls))} known dead")
    if args.one:
        todo = todo[:1]

    for number, url in enumerate(todo, 1):
        path = html_dir / f"{ad_id(url, site['id_pattern'])}.html"
        try:
            response = get(url)
        except requests.RequestException as error:
            print(f"  FAILED: {error}")
            continue

        if response.status_code == 404:
            with dead_file.open("a", encoding="utf-8") as fh:
                fh.write(f"{url}\n")
            continue

        if response.status_code != 200:
            continue

        path.write_text(response.text, encoding="utf-8")

        if args.one:
            # The listing page was a JavaScript shell with empty grey loading bars
            # where the jobs should be. Check this page is not the same before
            # committing to thousands of requests.
            skeletons = response.text.count("placeholder-content_item")
            print(f"\n  saved {path.relative_to(ROOT)}")
            print(f"  page size: {len(response.text):,} characters")
            print(f"  placeholder bars: {skeletons}")
            if skeletons:
                print("  EMPTY SHELL - stop, this needs a different approach")
            else:
                print("  real content - safe to run --all")

        if number % 25 == 0:
            print(f"  ... {number}/{len(todo)}")
        time.sleep(delay)

    print(f"\n  {len(list(html_dir.glob('*.html')))} ad pages on disk")
