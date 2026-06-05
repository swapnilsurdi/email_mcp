"""Security policy: recipient allowlist for sends, protected (read-only) folders, and
folder read access control. Configured in the accounts file under a top-level
`security:` section (see config/accounts.example.yml); absent config = permissive
defaults EXCEPT the trash protection, which is ON by default.

Semantics:
- `allowed_recipients`: list of case-insensitive FULL-MATCH regexes. When set, every
  send_email recipient must match one, or the send is BLOCKED before SMTP (and before
  the ledger — a policy block is never recorded). Plain addresses work as-is. An
  explicitly EMPTY list blocks all sends. Unset/None allows all (back-compat). Each
  recipient is first expanded to its bare address(es) via the same parser SMTP uses
  (email.utils.getaddresses), so a single element bundling extra addresses behind a
  comma or display name is validated address-by-address against the allowlist.
- Trash is ALWAYS protected (unless `protect_trash: false`): the reserved folder names
  of the major providers — Trash, Bin ([Gmail]/Bin), Deleted Messages (iCloud),
  Deleted Items (Outlook) — are matched as path segments, subfolders included. Since
  moving-to-trash is the only delete this server has, the default posture is that the
  MCP cannot delete mail at all. NOTE: a bare "Deleted" (some Exchange/O365/Dovecot
  setups name it just that) is NOT auto-matched — add it to `protected_folders` if your
  provider uses it.
- `protected_folders`: case-insensitive full-match regexes. A protected folder is
  READ-ONLY: nothing can be moved into or out of it, and messages in it cannot be
  flagged (mark) or expunged. Reading stays allowed.
- `readable_folders` / `blocked_folders`: case-insensitive full-match regexes gating
  reads (get_emails, download_attachment, folder listing, and the folder sets searched
  for mutations). When `readable_folders` is set, only matching folders are readable;
  `blocked_folders` always wins over it.
"""
import re
from email.utils import getaddresses

import yaml

# Reserved trash/bin names across providers, matched as a path segment (subfolders of
# trash are covered too). "Binary"/"Robin" do NOT match — the segment must be exact.
_TRASH_RE = re.compile(r"(^|/)(trash|bin|deleted\s+(items|messages))(/|$)",
                       re.IGNORECASE)


def _compile(patterns):
    if patterns is None:
        return None
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _fullmatch_any(compiled, value):
    return any(p.fullmatch(value) for p in compiled)


def expand_recipients(recipients):
    """Expand a recipient list into the bare addresses SMTP will actually deliver to.

    A single list element may carry a display name and/or several comma-separated
    addresses (e.g. "ok@x.com, other@y.com"). smtplib's send_message derives its
    envelope by re-parsing the joined To header the same way (email.utils.getaddresses),
    emitting one RCPT per address. So the policy must validate that same expanded set —
    otherwise a multi-address element would be checked as one opaque string yet delivered
    to every address inside it (a parsing-discrepancy / validation gap). getaddresses on
    a list joins with ', ' and parses, which is byte-identical to how the joined header is
    later parsed, so what we check here is exactly what gets sent."""
    return [addr for _name, addr in getaddresses(list(recipients)) if addr]


class SecurityPolicy:
    def __init__(self, allowed_recipients=None, protected_folders=None,
                 readable_folders=None, blocked_folders=None, protect_trash=True):
        self.allowed_recipients = _compile(allowed_recipients)
        self.protected_folders = _compile(protected_folders) or []
        self.readable_folders = _compile(readable_folders)
        self.blocked_folders = _compile(blocked_folders) or []
        self.protect_trash = protect_trash

    # ---- sends ------------------------------------------------------------------
    def denied_recipients(self, recipients):
        """The subset of `recipients` the policy does NOT allow. Empty list = all ok.
        Note: allowed_recipients == [] (explicitly empty) denies everyone."""
        if self.allowed_recipients is None:
            return []
        return [r for r in expand_recipients(recipients)
                if not _fullmatch_any(self.allowed_recipients, r.strip())]

    # ---- folders ----------------------------------------------------------------
    def folder_protected(self, folder):
        """Read-only folder: move in/out, mark, and expunge are refused."""
        if self.protect_trash and _TRASH_RE.search(folder or ""):
            return True
        return _fullmatch_any(self.protected_folders, folder or "")

    def folder_readable(self, folder):
        """Whether get_emails/download/listing may touch this folder."""
        f = folder or ""
        if _fullmatch_any(self.blocked_folders, f):
            return False
        if self.readable_folders is None:
            return True
        return _fullmatch_any(self.readable_folders, f)

    def filter_readable(self, folders):
        return [f for f in folders if self.folder_readable(f)]


def load_policy(accounts_file):
    """Build the policy from the accounts file's top-level `security:` section.
    A missing file or section yields the permissive default (trash still protected)."""
    try:
        with open(accounts_file) as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError):
        data = {}
    sec = data.get("security") or {}
    return SecurityPolicy(
        allowed_recipients=sec.get("allowed_recipients"),
        protected_folders=sec.get("protected_folders"),
        readable_folders=sec.get("readable_folders"),
        blocked_folders=sec.get("blocked_folders"),
        protect_trash=sec.get("protect_trash", True),
    )
