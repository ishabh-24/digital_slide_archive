"""
Read annotation files that already live on the server.

The upload and convert pages accept a server path instead of a browser
upload, because the datasets sit on the server (mounted into the Girder
container under /data) and the browser can only see the user's own machine.

A web route that opens arbitrary paths would expose everything the container
can read, so a path is accepted only when, after resolving symlinks, it lies
under one of the allowed roots, names a ``.json`` file, and is not huge.
The roots come from ``DSA_ANNOTATION_SOURCE_ROOTS`` (colon-separated),
defaulting to ``/data``.
"""

import os

DEFAULT_ROOTS = '/data'
MAX_BYTES = 256 * 1024 * 1024


def source_roots(environ=os.environ):
    return [root for root in environ.get('DSA_ANNOTATION_SOURCE_ROOTS', DEFAULT_ROOTS).split(':')
            if root.strip()]


def _under(real, root):
    root = os.path.realpath(root).rstrip('/') or '/'
    return real == root or real.startswith(root + '/')


def resolve_source_path(path, roots=None):
    """Return the real path if ``path`` may be read, else raise ValueError."""
    roots = roots if roots is not None else source_roots()
    path = (path or '').strip()
    if not path or not os.path.isabs(path):
        raise ValueError('server path must be absolute, e.g. '
                         '/data/BEETLE/annotations/jsons/patient1_wsi1.json')
    real = os.path.realpath(path)
    if not any(_under(real, root) for root in roots):
        raise ValueError('server path must be under %s (paths are as the Girder container '
                         'sees them: /mnt/raidData/BEETLE on the server is /data/BEETLE here)'
                         % ', '.join(roots))
    if not real.lower().endswith('.json'):
        raise ValueError('only .json files can be read from the server')
    if not os.path.isfile(real):
        raise ValueError('no such file on the server: %s' % path)
    if os.path.getsize(real) > MAX_BYTES:
        raise ValueError('file is larger than %d MB' % (MAX_BYTES // (1024 * 1024)))
    return real


def read_source_file(path, roots=None):
    with open(resolve_source_path(path, roots), encoding='utf-8') as handle:
        return handle.read()
