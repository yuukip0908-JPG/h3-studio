"""C_STUDIO's characters, read and never written.

H3 starts from a character (owner, 2026-09-29: 「トレースも１分も基本LORAから
作りたい」). The characters and the LoRAs that draw them live in C_STUDIO, and
H3 cannot load those LoRAs -- they belong to image models, and H3 is a video
model -- so what crosses between the two studios is the images.

This module is that crossing, and it only reads. Each Character Pack keeps a
manifest, CHARACTERS/<id>/character.json, listing its assets with a state;
C_STUDIO's own rules forbid breaking that schema, which is what makes it a
contract H3 can rely on. Only "accepted" images are offered, and nothing
outside a pack's own directory can be reached, whatever the manifest says.

Copying a chosen image into H3's uploads is the server's job, not this
module's: nothing here opens a file for anything but reading.
"""

import json
import os
import re

from . import config

MANIFEST = "character.json"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def root():
    return config.CSTUDIO_CHARACTERS_DIR


def available():
    return os.path.isdir(root())


def _pack_dir(char_id):
    """The pack's directory, or LookupError. `_template` and friends are not
    characters, and an id is never a path."""
    if not char_id or not _ID.match(char_id):
        raise LookupError("no such character")
    d = os.path.join(root(), char_id)
    if not os.path.isfile(os.path.join(d, MANIFEST)):
        raise LookupError("no such character")
    return d


def _manifest(char_id):
    with open(os.path.join(_pack_dir(char_id), MANIFEST), encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise LookupError("unreadable character")
    return data


def _inside(pack, rel):
    """The absolute path of `rel` if it is a file inside `pack`, else None."""
    if not isinstance(rel, str) or not rel.lower().endswith(IMAGE_EXTS):
        return None
    base = os.path.realpath(pack)
    path = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, path]) != base or not os.path.isfile(path):
        return None
    return path


def images(char_id):
    """Accepted images of one character whose files exist, newest first."""
    pack = _pack_dir(char_id)
    out = []
    for kind, items in ((_manifest(char_id).get("assets") or {}).items()):
        for a in items if isinstance(items, list) else ():
            if not isinstance(a, dict) or a.get("state") != "accepted":
                continue
            if not _inside(pack, a.get("path")):
                continue
            out.append({"asset_id": a.get("asset_id"), "kind": kind,
                        "label": a.get("label") or "",
                        "created_at": a.get("created_at") or ""})
    out.sort(key=lambda a: a["created_at"], reverse=True)
    return out


def image_path(char_id, asset_id):
    """The file behind one accepted image, or LookupError."""
    pack = _pack_dir(char_id)
    for items in (_manifest(char_id).get("assets") or {}).values():
        for a in items if isinstance(items, list) else ():
            if isinstance(a, dict) and a.get("asset_id") == asset_id \
                    and a.get("state") == "accepted":
                path = _inside(pack, a.get("path"))
                if path:
                    return path
    raise LookupError("no such image")


def characters():
    """Every character with at least a manifest, by name."""
    if not available():
        return []
    out = []
    for name in sorted(os.listdir(root())):
        if name.startswith(("_", ".")):
            continue
        try:
            m = _manifest(name)
            imgs = images(name)
        except (LookupError, OSError, ValueError):
            continue
        if m.get("status") not in (None, "active"):
            continue
        out.append({"id": name, "name": m.get("name") or name,
                    "image_count": len(imgs),
                    "cover": imgs[0]["asset_id"] if imgs else None})
    out.sort(key=lambda c: c["name"])
    return out


def describe(char_id):
    m = _manifest(char_id)
    return {"id": char_id, "name": m.get("name") or char_id}
