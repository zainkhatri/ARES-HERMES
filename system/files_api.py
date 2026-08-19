"""Read-only file explorer: confined, vault-excluded filesystem access.

All filesystem paths MUST pass through safe_resolve() before use. Read-only:
this module never writes, renames, or deletes.
"""
import os
from system.system_info import POOL_ROOT

ROOT = os.path.realpath(POOL_ROOT)
assert os.path.isdir(ROOT), f"files_api: POOL_ROOT not a directory: {ROOT!r}"

# Defense-in-depth denylist (dotfiles are also hidden, which covers .vault).
DENY_NAMES = frozenset({
    ".vault", "vault_enc", "vault_thumbs", "vault_thumbs_hq",
    "vault_video_cache", "vault_hls", "My Eyes Only",
})


def safe_resolve(rel, root=ROOT):
    """Return the confined absolute realpath for user-supplied rel, else None.

    Confinement + vault/dotfile checks run on the RESOLVED realpath so a
    symlink pointing outside root or into the vault fails closed.
    """
    assert isinstance(rel, str), "rel must be str"
    assert isinstance(root, str) and root, "root must be non-empty str"
    if "\x00" in rel or os.path.isabs(rel):
        return None
    root = os.path.realpath(root)
    abspath = os.path.realpath(os.path.join(root, rel))
    if abspath != root and not abspath.startswith(root + os.sep):
        return None
    inside = abspath[len(root):]                      # "" at root, else "/a/b"
    for seg in inside.split(os.sep):
        if not seg:
            continue
        if seg in DENY_NAMES or seg.startswith("."):
            return None
    return abspath
