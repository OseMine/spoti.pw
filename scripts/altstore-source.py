#!/usr/bin/env python3
"""Writes the AltStore source.json for a release, from a built IPA's identity and the official notes.

  scripts/altstore-source.py <identity.json> --download-url URL [options]

    --app PATH         static metadata        (altstore/app.json)
    --out PATH         where source.json goes (altstore/site/source.json)
    --base-url URL     where the site is served ($ALTSTORE_BASE_URL)
    --mod-version V    the mod's version      (version.txt)
    --notes TEXT       release notes          (fetched from the official repo by default)
    --date ISO8601     the version's date     (the official release's, else now)
    --previous URL|PATH  a source.json whose history is kept (altstore/site/source.json)
    --keep N           how many versions to publish (10)

<identity.json> is what scripts/altstore-ipa.py prints for the IPA being published, so the version
entry AltStore verifies carries exactly that IPA's `version` and `buildVersion`, its size, and its
sha256. `appPermissions` is built from the identity too: AltStore 2.0 refuses to install an app that
asks for an entitlement or a privacy string the source does not declare, so the two have to agree,
and reading both out of the same IPA is what makes them.

Every entry keeps Spotify's `version` (9.1.78) and differs by `buildVersion`, which is how AltStore
already tells releases of the same app apart (its AppVersion is unique on appBundleID + version +
buildVersion + sourceID). The notes are the official spoti.pw ones for the same mod version, falling
back to the newest official release, because this source only mirrors what upstream publishes.
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://spotipw-site.vercel.app/"
DEFAULT_KEEP = 10
NOTEBOOK = "https://github.com/{repo}/releases"


def arguments(argv):
    """--key value pairs, plus the one positional identity file."""
    values = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            values[token[2:]] = argv[index + 1]
            index += 2
        else:
            values.setdefault("identity", token)
            index += 1
    return values


def read_json(path):
    with open(path) as f:
        return json.load(f)


def fetch_json(url, token=None):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "spoti.pw-altstore"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def official_release(repo, version):
    """The official spoti.pw release for `version`, the newest one, or None."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    releases = f"https://api.github.com/repos/{repo}/releases"
    for endpoint in (f"{releases}/tags/v{version}", f"{releases}/tags/{version}", f"{releases}/latest"):
        try:
            return fetch_json(endpoint, token)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                print(f"    {endpoint}: {error}", file=sys.stderr)
        except Exception as error:  # offline, rate limited, DNS
            print(f"    {endpoint}: {error}", file=sys.stderr)
            return None
    return None


def timestamp(value):
    """Now, or an ISO 8601 string as AltStore parses it (full date, or date and time with a zone)."""
    if value:
        return value
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def caption(notes):
    """A one-line news caption: the first entry of the release notes, its links unwrapped."""
    def clean(line):
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # [text](url) -> text
        line = re.sub(r"\s*\([0-9a-f]{7,}\)", "", line)       # a bare commit hash
        return re.sub(r"^[#*>\\\-\s]+", "", line).strip()

    bullets = [clean(l) for l in notes.splitlines() if l.lstrip().startswith(("*", "-"))]
    lines = [clean(l) for l in notes.splitlines()]
    for line in bullets + lines:
        if line:
            return line[:200]
    return ""


def tint(value):
    """AltStore's sources write these without the leading hash; UIColor(hexString:) is fed the bare
    digits, so keep it that way whatever the metadata says."""
    return value.lstrip("#") if value else value


def absolute(url, base, what):
    """AltStore decodes these as URLs, so a relative one would fail the whole source to load."""
    if not url:
        return None
    if re.match(r"^https?://", url):
        return url
    if base:
        resolved = urllib.parse.urljoin(base, url)
        if re.match(r"^https?://", resolved):
            return resolved
    sys.exit(f"{what}: {url!r} is not an http(s) URL")


def main():
    args = arguments(sys.argv[1:])
    if "identity" not in args or "download-url" not in args:
        sys.exit(__doc__.split("\n\n")[1])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    identity = read_json(args["identity"])
    app = read_json(args.get("app", os.path.join(root, "altstore/app.json")))
    repo = app.pop("releaseNotesRepo", None)
    app.pop("//", None)

    version = args.get("mod-version")
    if version is None:
        with open(os.path.join(root, "version.txt")) as f:
            version = f.read().strip()
    base = args.get("base-url") or os.environ.get("ALTSTORE_BASE_URL") or DEFAULT_BASE_URL
    base = base if base.endswith("/") else base + "/"

    release = official_release(repo, version) if repo else None
    notes, notes_from = args.get("notes"), "the command line"
    if notes is None and release:
        notes, notes_from = release.get("body"), f"the official {version} release"
    notes = (notes or "").strip()
    if not notes:
        notes = f"See the official release notes at {NOTEBOOK.format(repo=repo or 'skopevoj/spoti.pw')}."
        notes_from = "a fallback"

    published = args.get("date") or (release or {}).get("published_at")
    template = app["apps"][0]

    new = {
        "version": identity["version"],
        "buildVersion": identity["buildVersion"],
        "date": timestamp(published),
        "localizedDescription": notes,
        "downloadURL": absolute(args["download-url"], None, "download-url"),
        "size": identity["size"],
        "sha256": identity["sha256"],
    }
    minimum = template.pop("minOSVersion", None)
    if minimum:
        new["minOSVersion"] = minimum

    # Carry the releases already published, so the source keeps its history across refreshes. An
    # entry is identified the way AltStore's AppVersion is unique, so a republished build replaces
    # its own entry rather than adding a second. The file the workflow commits is the history: it is
    # read from the checkout rather than from the deployed site.
    ref = args.get("previous")
    previous = []
    if ref:
        try:
            history = fetch_json(ref) if re.match(r"^https?://", ref) else read_json(ref)
            previous = history["apps"][0]["versions"]
        except Exception as error:
            print(f"    no history from {ref}: {error}", file=sys.stderr)

    def identity_of(entry):
        return (entry["version"], entry.get("buildVersion"))

    entries = [new] + [e for e in previous if identity_of(e) != identity_of(new)]
    entries = entries[:int(args.get("keep", DEFAULT_KEEP))]

    permissions = {"entitlements": [{"name": name} for name in identity["entitlements"]],
                   "privacy": identity["privacy"]}

    template.update({
        "iconURL": absolute(template.get("iconURL"), base, "app iconURL"),
        "screenshotURLs": [absolute(u, base, "app screenshotURLs") for u in template.get("screenshotURLs") or []],
        "versions": entries,
    })
    if template.get("tintColor"):
        template["tintColor"] = tint(template["tintColor"])
    if any(permissions.values()):
        template["appPermissions"] = {k: v for k, v in permissions.items() if v}

    source = {}
    for key, value in app.items():
        if key == "apps":
            continue
        if key in ("iconURL", "headerURL"):
            value = absolute(value, base, key)
        elif key == "tintColor":
            value = tint(value)
        source[key] = value
    source["apps"] = [template]
    source["news"] = [{
        "title": f"spoti.pw {version}",
        "identifier": f"spoti.pw-{version}",
        "caption": caption(notes),
        "tintColor": template.get("tintColor", "1ED760"),
        "date": timestamp(published),
        "notify": False,
    }]

    out = args.get("out", os.path.join(root, "altstore/site/source.json"))
    with open(out, "w") as f:
        json.dump(source, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"    spoti.pw {version} (Spotify {new['version']} build {new['buildVersion']}) -> {out}")
    print(f"    {len(entries)} versions, {len(permissions['entitlements'])} entitlements, "
          f"{len(permissions['privacy'])} privacy strings, notes from {notes_from}")


if __name__ == "__main__":
    main()
