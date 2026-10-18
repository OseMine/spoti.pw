# AltStore / SideStore source

AltStore 2.0 refuses to install an app whose source does not declare something the app asks for,
so the source and the IPA have to agree. Reading `appPermissions` out of the same IPA that gets
published is what makes them agree; nothing about the source is maintained by hand.

The mod is installed from a source URL rather than a GitHub release, so it can update itself: the
IPA is attached to the release, and `source.json` is served from Vercel.

## What lives here

    altstore/app.json               the parts of the source that do not change per release
    altstore/site/                  what Vercel serves: index.html, icon.png, vercel.json, and the
                                    source.json the workflow commits
    scripts/altstore-ipa.py         reads the identity AltStore checks, and stamps CFBundleVersion
    scripts/altstore-source.py      writes source.json from app.json + identity + release notes
    .github/workflows/altstore.yml  all of it, as a reusable workflow release.yml calls

There is nothing to upload anywhere and nothing to keep in step by hand: every release commits the
new `source.json`, and the push is what deploys the site.

## One-time setup

**A Vercel project** for the site: import the repository, name it what the address should be (a
project called `spotipw-site` answers at `spotipw-site.vercel.app`), set the root directory to
`altstore/site`, and leave the framework and build command empty — it is static files. Connect Git,
so a push to `main` deploys; the workflow commits `source.json` rather than deploying, so nothing
else is needed here.

Note that `source.json` only appears after the first release is published — until then the address
serves the page and its icon, and `source.json` is a 404.

**Secrets** (repository → Settings → Secrets and variables → Actions):

| Secret | What it is |
| --- | --- |
| `SPOTIFY_IPA_URL` | a direct link to a decrypted Spotify IPA, fetched with `curl -fL` and no auth — put a token in the URL if it needs one |
| `SG_FLAG_LIST` | the flag table from a real Spotify binary, the same secret `release.yml` uses (`gzip -9c tweak/Sources/Shared/Flags/SGFlagList.m \| base64 \| gh secret set SG_FLAG_LIST`); optional here, since the IPA is available to extract it from |

**A variable** (same page, Variables tab), only if the site is on a custom domain: `ALTSTORE_BASE_URL`,
the site's own URL with the trailing slash (`https://altstore.spoti.pw/`). It resolves the source's
relative `iconURL`; unset, it is the Vercel address `altstore-source.py` defaults to.

## Why the IPA is a release asset

It is the only public place it fits. A Vercel Hobby deployment carries 100 MB and the IPA is
bigger, and GitHub refuses any file over 100 MB, so it cannot be deployed and it cannot be in the
repository — where it would also sit in history forever, one copy per release. Release assets hold
2 GB without complaint and download without a token, which is what AltStore needs. **This is why the
repository has to stay public**: a private repository's release assets need authentication, and
AltStore sends none. If it ever goes private, the IPA needs a host of its own (Vercel Blob, say) and
`--download-url` points at that instead.

## What a release does

`.github/workflows/release.yml` calls `.github/workflows/altstore.yml` as a reusable workflow from
the job that runs when Release Please has cut a release. It is called rather than triggered by
`release: published` because a workflow's `GITHUB_TOKEN` starts no other workflow: the release
Release Please makes would never fire that event. (The trigger is still there for a release published
by hand, and `workflow_dispatch` republishes a version.)

Given the release, it checks out the tag, builds the same IPA the `.deb` comes from and then:

1. `scripts/altstore-ipa.py out/*.ipa --stamp-release <version>` stamps the release's
   `CFBundleVersion` into the app and every `*.appex`, and prints the resulting identity as JSON —
   `version`, `buildVersion`, `size`, `sha256`, `entitlements`, `privacy`.
2. `gh release upload` attaches that IPA to the release as `spoti.pw-<version>.ipa`.
3. `scripts/altstore-source.py` writes `altstore/site/source.json` from `altstore/app.json`, the
   identity, the official release notes for the same version, and the `source.json` already
   committed — which is the history it keeps. Nothing is read back from the live site.
4. The new `source.json` is committed to `main` as `chore(altstore): source.json for <version>
   [skip ci]`. Vercel builds the push and the source updates.

Trigger it by hand (`workflow_dispatch`) to publish or republish a version: `tag` says which release
to publish into (default `v<version.txt>`), `ipa_url` overrides `SPOTIFY_IPA_URL`, and `mod_version`
overrides `version.txt`. The release has to exist, since the IPA is attached to it. Re-running a
version is safe — the release asset is clobbered and the version entry it produces replaces its own.

## The build number

AltStore decides there is an update when the source's `version` or `buildVersion` differs from what
is installed. Spotify keeps `CFBundleShortVersionString` (9.1.78) and `CFBundleVersion` (1) forever,
so a source that mirrors them would be invisible to AltStore. Every release therefore stamps its own
`CFBundleVersion`, `major*10000 + minor*100 + patch` of `version.txt` (0.22.0 → `2200`), which
Release Please makes distinct by bumping `version.txt`. Versions can go past `2200`; they only have
to rise, and they stop fitting once a minor or patch passes 99.

Only `CFBundleVersion` is written. `cyan -v` would also rewrite `CFBundleShortVersionString`, which
is what the mod's About page shows and what AltStore matches `version` against, so it is not used.
The extensions are stamped with the app because `scripts/build-extension.sh` copies the host's
values into them, and a mismatch there is a source AltStore refuses.

## Testing locally

    python scripts/altstore-ipa.py out/spoti.pw-0.22.0.ipa --summary
    python scripts/altstore-ipa.py out/spoti.pw-0.22.0.ipa --stamp-release 0.22.0 > /tmp/identity.json
    python scripts/altstore-source.py /tmp/identity.json \
      --download-url "https://github.com/<owner>/<repo>/releases/download/v0.22.0/spoti.pw-0.22.0.ipa" \
      --previous altstore/site/source.json

`altstore-source.py` fetches the notes from `skopevoj/spoti.pw` anonymously, or uses `--notes`.
Both scripts are also useful on their own: `--stamp-release`/`--set-build-version` for an IPA you
built by hand, and the identity JSON for seeing exactly what AltStore will check before publishing.

`source.json` is validated by AltStore's own decoder: every URL has to be absolute, `version` and
`buildVersion` have to equal the IPA's, `sha256` is verified against the download, and every
entitlement and `NS*UsageDescription` the app and its extensions declare has to be listed in
`appPermissions`. `altstore-source.py` builds `appPermissions` from the identity for that reason —
editing `app.json`'s app entry changes metadata, but the permissions should stay generated.

## Limits

- The IPA is unsigned, which is the point: AltStore re-signs it with the user's certificate on
  install, and the bundle id stays `com.spotify.client` so it replaces the real Spotify.
- The source is bound to one decrypted Spotify version. `version` in every entry is whatever the
  IPA at `SPOTIFY_IPA_URL` says, so replacing that with an IPA of a different Spotify version
  changes every entry's `version` at once and installs will be offered again — update the secret
  deliberately.
- No Spotify IPA is distributed here beyond the one the person signing already has.
