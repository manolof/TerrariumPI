# -*- coding: utf-8 -*-
"""Where a static file request may read from.

Kept apart from the webserver so it can be tested on its own: the webserver module needs bottle and
the engine, which the tests replace with stubs.
"""
import os

# Folders under public/ that the old /static/<folder>/... addresses may name
STATIC_FOLDERS = ("api", "css", "extern", "img", "js", "webfonts")


def static_target(filename: str, root: str):
    """The (root, filename) to serve, or None when the request reaches outside its root.

    `/static/<folder>/<file>` maps to `public/<folder>/<file>`, and only for the folders above. Any
    path whose resolved file is not inside its resolved root is refused, so `..` in any form never
    leaves the root.
    """
    if root == "static":
        parts = filename.split("/")
        if len(parts) < 2 or parts[0] not in STATIC_FOLDERS:
            return None
        root = f"public/{parts[0]}"
        filename = "/".join(parts[1:])

    if filename in ("", ".") or "\x00" in filename:
        return None

    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, filename))
    if not target.startswith(base + os.sep):
        return None

    return root, filename
