# AltStore / SideStore source

AltStore 2.0 refuses to install an app whose source does not declare something the app asks for,
so the source and the IPA have to agree. Reading `appPermissions` out of the same IPA that gets
published is what makes them agree; nothing about the source is maintained by hand.

The mod is installed from a source URL rather than a GitHub release, so it can update itself: the
repository stays private, and what is public is only `source.json` and the IPA it points at, both
served from Vercel.

## What lives here

    altstore/app.json               the parts of the source that do not change per release
    altstore/publish.mjs            uploads the IPA to Vercel Blob, prints its public URL
    altstore/package.json           @vercel/blob, pinned (the only Node in the repo)
    altstore/site/                  what Vercel serves: index.html, icon.png, vercel.json, and the
                                    source.json the workflow writes there (gitignored)
    scripts/altstore-ipa.py         reads the identity AltStore checks, and stamps CFBundleVersion
    scripts/altstore-source.py      writes source.json from app.json + identity + release notes
    .github/workflows/altstore.yml  all of it, on every published release

`source.json` is generated, never committed: the history it keeps is read back from the published
copy, so the live site is the only state.

## One-time setup

**A Vercel project** for the site: import the repo, name it what the address should be (a project
called `spoti-pw-altstore` answers at `spoti-pw-altstore.vercel.app`), set the root directory to
`altstore/site`, and leave the framework and build command empty — it is static files. The workflow
passes `--cwd altstore/site` itself, so the setting only matters if you also connect Git.

**A Vercel Blob store** for the IPAs. They are larger than a Hobby static deployment carries (100 MB),
and Blob takes multipart uploads. Connect the store to the project; that is what puts
`BLOB_READ_WRITE_TOKEN` in the project's environment.

**Secrets** (repository → Settings → Secrets and variables → Actions):

| Secret | What it is |
| --- | --- |
| `SPOTIFY_IPA_URL` | a direct link to a decrypted Spotify IPA, fetched with `curl -fL` and no auth — put a token in the URL if it needs one |
| `SG_FLAG_LIST` | the flag table from a real Spotify binary, the same secret `release.yml` uses (`gzip -9c tweak/Sources/Shared/Flags/SGFlagList.m \| base64 \| gh secret set SG_FLAG_LIST`); optional here, since the IPA is available to extract it from |
| `VERCEL_TOKEN` | vercel.com/account/tokens |
| `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | from `.vercel/project.json` after `vercel link` in `altstore/site` (`orgId`, `projectId`) |
| `BLOB_READ_WRITE_TOKEN` | the Blob store's read/write token |

**A variable** (same page, Variables tab): `ALTSTORE_BASE_URL`, the site's own URL, with the
trailing slash (`https://spoti-pw-altstore.vercel.app/`). It resolves the source's relative
`iconURL` and is where the previous `source.json` is fetched from for history, so a wrong value
means no history rather than a failed release.

## What a release does

`.github/workflows/altstore.yml` runs on `release: published`, checks out the tag, builds the same
IPA the `.deb` comes from and then:

1. `scripts/altstore-ipa.py out/*.ipa --stamp-release <version>` stamps the release's
   `CFBundleVersion` into the app and every `*.appex`, and prints the resulting identity as JSON —
   `version`, `buildVersion`, `size`, `sha256`, `entitlements`, `privacy`.
2. `altstore/publish.mjs` uploads that IPA to Blob as `spoti.pw-<version>.ipa`.
3. `scripts/altstore-source.py` writes `altstore/site/source.json` from `altstore/app.json`, the
   identity, the official release notes for the same version, and the history already published.
4. `vercel deploy --prod --cwd altstore/site` publishes it.

Trigger it by hand (`workflow_dispatch`) to test: `ipa_url` overrides `SPOTIFY_IPA_URL`, and
`mod_version` overrides `version.txt`. Re-running a version is safe — the Blob pathname is stable,
and the version entry it produces replaces its own.

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

    npm ci --prefix altstore
    node altstore/publish.mjs out/spoti.pw-0.22.0.ipa spoti.pw-0.22.0.ipa      # prints the URL
    python scripts/altstore-ipa.py out/spoti.pw-0.22.0.ipa --summary
    python scripts/altstore-ipa.py out/spoti.pw-0.22.0.ipa --stamp-release 0.22.0 > /tmp/identity.json
    python scripts/altstore-source.py /tmp/identity.json \
      --download-url "https://<store>.public.blob.vercel-storage.com/spoti.pw-0.22.0.ipa" \
      --previous "https://<address>/source.json"

`publish.mjs` needs `BLOB_READ_WRITE_TOKEN` in the environment; `altstore-source.py` fetches the
notes from `skopevoj/spoti.pw` anonymously, or uses `--notes`. Both are also useful on their own:
`--stamp-release`/`--set-build-version` for an IPA you built by hand, and the identity JSON for
seeing exactly what AltStore will check before publishing.

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
