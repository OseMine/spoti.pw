#!/usr/bin/env python3
"""Reads a built IPA the way AltStore does, and can stamp a release build number into it.

  scripts/altstore-ipa.py <ipa> [--stamp-release 0.22.0] [--set-build-version N] [--summary]

Prints, as JSON on stdout (a human summary with --summary), the identity AltStore sees:

  bundleIdentifier  what the source has to name
  version           CFBundleShortVersionString, which stays Spotify's own (9.1.78)
  buildVersion      CFBundleVersion, the per-release number this can stamp
  minimumOSVersion, size, sha256
  entitlements      every entitlement key the binaries' code signatures carry
  privacy           every NS*UsageDescription key the app and its extensions declare

AltStore 2.0 checks a downloaded IPA against its source entry before installing it: `version` and
`buildVersion` have to match exactly, `sha256` too when the source gives one, and every entitlement
and privacy string the app asks for has to be declared by the source or the install is refused
(VerifyAppOperation.verifyPermissions throws undeclaredPermissions, and only in BETA can it be
ignored). So this reads the identity out of the very IPA that is published. The entitlements are
read with ldid's own rule -- the first CSSLOT_ENTITLEMENTS blob of the first Mach-O slice that has
one -- because that is how AltSign reads them for AltStore; reading them the same way is what keeps
the two in agreement.

Spotify's own CFBundleVersion never changes between releases, so a source and an installed app
would both read 9.1.78 (same build) no matter how far the mod had moved on, and AltStore would
never offer an update. --stamp-release derives a number from the mod version (0.22.0 -> 2200) and
writes it into the app and each of its extensions, so every release has a buildVersion of its own.
Only CFBundleVersion is touched: CFBundleShortVersionString stays Spotify's, which is what the
mod's About page shows and what AltStore matches `version` against.
"""
import hashlib
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import zipfile

# A code signature superblob, and the slot of the XML entitlements inside it. ldid looks only at
# this slot; an IPA signed with DER entitlements alone therefore reports no entitlements, and
# AltStore agrees because it asks ldid.
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSSLOT_ENTITLEMENTS = 0x5
LC_CODE_SIGNATURE = 0x1D

FAT_MAGIC = (0xCAFEBABE, 0xCAFEBABF)
MH_MAGIC_64 = 0xFEEDFACF
MH_MAGIC = 0xFEEDFACE

# The two AltSign leaves out of a permission check, so the source should not declare them either.
IGNORED_ENTITLEMENTS = ("application-identifier", "com.apple.developer.team-identifier")

# AltStore's own rule for a privacy key: NS<name>UsageDescription<suffix>, matched whole.
PRIVACY_KEY = re.compile(r"NS(.+)UsageDescription(.*)", re.DOTALL)


def slices(data):
    """Offsets of the Mach-O slices of a thin or fat binary."""
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in FAT_MAGIC:
        wide = magic == FAT_MAGIC[1]
        count = struct.unpack_from(">I", data, 4)[0]
        step = 32 if wide else 20
        for i in range(count):
            fmt = ">Q" if wide else ">I"
            yield struct.unpack_from(fmt, data, 8 + i * step + 8)[0]
        return
    yield 0


def entitlements_at(data, base):
    """The XML entitlements of the slice at `base`, or None."""
    magic = struct.unpack_from("<I", data, base)[0]
    if magic == MH_MAGIC_64:
        header, is64 = 32, True
    elif magic == MH_MAGIC:
        header, is64 = 28, False
    else:
        return None

    ncmds = struct.unpack_from("<I", data, base + 16)[0]
    off = base + header
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == LC_CODE_SIGNATURE:
            return entitlements_blob(data, base + struct.unpack_from("<I", data, off + 8)[0])
        off += cmdsize
    return None


def entitlements_blob(data, top):
    """The first non-empty CSSLOT_ENTITLEMENTS payload of the signature at `top`, or None."""
    magic, _, count = struct.unpack_from(">III", data, top)
    if magic != CSMAGIC_EMBEDDED_SIGNATURE:
        magic, _, count = struct.unpack_from("<III", data, top)
        if magic != CSMAGIC_EMBEDDED_SIGNATURE:
            return None
        endian = "<"
    else:
        endian = ">"

    for i in range(count):
        kind, offset = struct.unpack_from(endian + "II", data, top + 12 + i * 8)
        if kind != CSSLOT_ENTITLEMENTS:
            continue
        length = struct.unpack_from(endian + "I", data, top + offset + 4)[0]
        blob = data[top + offset + 8:top + offset + length]
        if blob:
            return blob
    return None


def entitlements_of(data):
    """The entitlement keys of a Mach-O, the way ldid would hand them to AltStore."""
    for base in slices(data):
        blob = entitlements_at(data, base)
        if not blob:
            continue
        try:
            entitlements = plistlib.loads(blob)
        except Exception:
            continue
        if entitlements:
            return [key for key in entitlements if key not in IGNORED_ENTITLEMENTS]
    return []


def entry(zf, path):
    """The archive's own spelling of `path`, which is not always the same case."""
    lowered = path.lower()
    for name in zf.namelist():
        if name.lower() == lowered:
            return name
    return None


def app_dir(zf):
    for name in zf.namelist():
        parts = name.split("/")
        if len(parts) > 2 and parts[0] == "Payload" and parts[1].endswith(".app"):
            return "/".join(parts[:2]) + "/"
    sys.exit("no Payload/*.app in the IPA")


def executable(zf, bundle):
    """The Mach-O of a .app/.appex, from its CFBundleExecutable."""
    plist = entry(zf, bundle + "Info.plist")
    if plist is None:
        return None
    name = plistlib.loads(zf.read(plist)).get("CFBundleExecutable")
    return entry(zf, bundle + name) if name else None


def bundles(zf, app):
    """The app bundle and its extensions, the two AltStore checks permissions across."""
    found = [app]
    for name in zf.namelist():
        parts = name.split("/")
        prefix = app + "PlugIns/"
        if name.startswith(prefix) and len(parts) == 4 and parts[3].endswith(".appex"):
            found.append("/".join(parts[:4]) + "/")
    return found


def privacy_of(plists):
    """{name: usageDescription} for every NS*UsageDescription key AltStore would demand."""
    found = {}
    for plist in plists:
        for key, value in plist.items():
            match = PRIVACY_KEY.fullmatch(key) if isinstance(key, str) else None
            if match:
                found.setdefault(match.group(1), value if isinstance(value, str) else "")
    return found


def build_number(version):
    """The CFBundleVersion for a mod release: 0.22.0 -> 2200, and ordered like the version is."""
    parts = [int(part) for part in re.findall(r"\d+", version)[:3]]
    parts += [0] * (3 - len(parts))
    if len(parts) != 3 or any(part > 99 for part in parts[1:]):
        sys.exit(f"cannot make a build number out of {version!r}")
    return int(f"{parts[0]}{parts[1]:02d}{parts[2]:02d}")


def stamp(ipa, number):
    """Writes CFBundleVersion into the app and every extension, in place."""
    with zipfile.ZipFile(ipa) as zf:
        members = [name for name in zf.namelist() if name.lower().endswith(".appex/info.plist")]
        members.append(app_dir(zf) + "Info.plist")
        plists = {member: plistlib.loads(zf.read(member)) for member in members}

    with tempfile.TemporaryDirectory() as tmp:
        for member, plist in plists.items():
            plist["CFBundleVersion"] = number
            path = os.path.join(tmp, member)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                plistlib.dump(plist, f, fmt=plistlib.FMT_BINARY)
        # zip replaces the entries it is given, the way scripts/merge-appintents.py relies on.
        subprocess.run(["zip", "-q", os.path.abspath(ipa), *plists], cwd=tmp, check=True)
    print(f"    CFBundleVersion {number} written to the app and {len(plists) - 1} extensions")


def identity(ipa):
    with zipfile.ZipFile(ipa) as zf:
        app = app_dir(zf)
        plists = {}
        for bundle in bundles(zf, app):
            plist = entry(zf, bundle + "Info.plist")
            if plist is not None:
                plists[bundle] = plistlib.loads(zf.read(plist))

        entitlements = []
        for bundle in plists:
            binary = executable(zf, bundle)
            if binary is not None:
                entitlements += entitlements_of(zf.read(binary))

        main = plists[app]
        seen, unique = set(), []
        for key in entitlements:
            if key not in seen:
                seen.add(key)
                unique.append(key)

        privacy = [{"name": name, "usageDescription": text}
                   for name, text in sorted(privacy_of(plists.values()).items())]

    digest = hashlib.sha256()
    with open(ipa, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)

    return {
        "bundleIdentifier": main.get("CFBundleIdentifier"),
        "name": main.get("CFBundleDisplayName") or main.get("CFBundleName"),
        "version": main.get("CFBundleShortVersionString", "1.0"),
        "buildVersion": main.get("CFBundleVersion", "1"),
        "minimumOSVersion": main.get("MinimumOSVersion"),
        "size": os.path.getsize(ipa),
        "sha256": digest.hexdigest(),
        "entitlements": unique,
        "privacy": privacy,
    }


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__.split("\n\n")[1])

    ipa, stamp_with, summary = argv[0], None, "--summary" in argv
    for flag in ("--stamp-release", "--set-build-version"):
        if flag in argv:
            value = argv[argv.index(flag) + 1]
            stamp_with = build_number(value) if flag == "--stamp-release" else value

    if stamp_with is not None:
        stamp(ipa, str(stamp_with))

    found = identity(ipa)
    if summary:
        for key, value in found.items():
            if key == "privacy":
                value = "; ".join(f"{p['name']} ({len(p['usageDescription'])} chars)" for p in value)
            if key == "entitlements":
                value = ", ".join(value) or "none"
            print(f"    {key}: {value}")
    else:
        json.dump(found, sys.stdout, indent=2, sort_keys=True)
        print()


if __name__ == "__main__":
    main()
