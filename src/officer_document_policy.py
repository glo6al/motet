"""Closed, dated qualification for Motet original PDF/PNG input.

A local policy selects evidence and owner allowance; it cannot invent pricing,
model support, token bounds or expiry. Nothing here enables a feature or sends.
"""
from dataclasses import dataclass, replace
import json
import math
import os
import time

import private_storage as ps
from attachments_files import identity, private
from attachments_types import OPAQUE, fail
import officer_documents as documents

# Renewed 2026-11-24 after the sources below were checked again that day: prices, tiers, routing, context window and
# PDF limit are unchanged, so only the evidence date and its window move. The 2026-11-15 qualification ended at
# 2026-11-22T00:00:00Z.
EVIDENCE_ID = 'haiku-pdf-png-2026-11-24'
VALID_FROM = 1795478400  # 2026-11-24T00:00:00Z
VALID_UNTIL = 1798070400  # 2026-12-24T00:00:00Z, exclusive
PROFILE_REVISION = 'haiku-direct-pdf-png-2026-11'
PRICING_REVISION = 'haiku-tiered-2026-11-24'
BILLING_REVISION = 'minimal-standard-default-global-or-us-v1'
MAX_INPUT = 1_000_000
LOW_THRESHOLD = 100_000
PER_CALL_USD = '1.11'
POLICY_FILENAME = 'officer-document-policy.json'
SOURCES = ('https://platform.claude.com/docs/en/build-with-claude/context-windows',
           'https://platform.claude.com/docs/en/models/haiku-5-5/overview',
           'https://platform.claude.com/docs/en/about-claude/pricing')
# Integer nanodollars per token, ordered input/output/cache read/5m/1h.
HIGH = (500, 2500, 50, 625, 1000)
LOW = (100, 500, 10, 125, 200)
# Earlier qualifications whose stored budget carries forward into this one: a closed list of exact ids, never a
# pattern. Each entry records every term that build priced with. It is honoured only while those terms equal today's,
# so a later price change ends the carry-forward instead of re-pricing an old budget. Carrying forward never touches
# the stored budget: its scope, limit and every recorded charge stay as they are (see officer_document_ledger).
PREDECESSORS = (
    dict(evidence_id='haiku-pdf-png-2026-11-15', pricing_revision='haiku-tiered-2026-11-15',
         profile_revision='haiku-direct-pdf-png-2026-11', billing_revision='minimal-standard-default-global-or-us-v1',
         high=(500, 2500, 50, 625, 1000), low=(100, 500, 10, 125, 200), low_threshold=100_000,
         max_input=1_000_000, per_call_usd='1.11'),
)


def unavailable():
    fail('document_policy_unavailable', 'Motet file reading is unavailable on this host.', 503)


def current(clock=time.time):
    stamp = clock()
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not VALID_FROM <= stamp < VALID_UNTIL:
        unavailable()


def input_profile():
    return _profile_for(EVIDENCE_ID, PRICING_REVISION)


def _profile_for(evidence_id, pricing_revision):
    proof = documents.StrictBudgetEvidence('strict_usd', pricing_revision, BILLING_REVISION,
        PER_CALL_USD, 'documented-context-maximum', evidence_id, MAX_INPUT, '0'*64)
    profile = documents.OfficerInputProfile('officer-direct-pdf-png', 'v1', PROFILE_REVISION,
        documents.MODEL, 'minimal-direct-v1', ('application/pdf', 'image/png'), proof)
    return replace(profile, strict_budget_evidence=replace(proof, profile_binding=documents.profile_binding(profile)))


def reserve_units():
    # Every consumed input token could be the most expensive cache class.
    global_units = MAX_INPUT * max(HIGH[0], *HIGH[2:]) + 1024 * HIGH[1]
    return (global_units * 11 + 9) // 10


def usage_units(usage):
    """Conservative dated rate estimate, not a provider invoice or a refund."""
    if type(usage) is not documents.DocumentUsage:
        unavailable()
    values = (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens,
              usage.cache_5m_tokens, usage.cache_1h_tokens)
    if (any(type(v) is not int or v < 0 for v in values)
            or type(usage.cache_creation_input_tokens) is not int
            or usage.cache_creation_input_tokens != usage.cache_5m_tokens + usage.cache_1h_tokens
            or type(usage.thinking_tokens) is not int or not 0 <= usage.thinking_tokens <= usage.output_tokens
            or usage.output_tokens > 1024 or usage.service_tier != 'standard'
            or usage.speed != 'standard' or usage.inference_geo not in ('global', 'us')):
        unavailable()
    consumed = usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    if consumed > MAX_INPUT:
        unavailable()
    rates = LOW if consumed <= LOW_THRESHOLD else HIGH
    units = sum(v*r for v, r in zip(values, rates))
    return units if usage.inference_geo == 'global' else (units*11+9)//10


@dataclass(frozen=True)
class HostAllowance:
    evidence_id: str
    scope_id: str
    scope_revision: str
    usd_limit: str

    def validate(self, clock=time.time):
        current(clock)
        if (self.evidence_id != EVIDENCE_ID
                or any(type(v) is not str or not OPAQUE.fullmatch(v) for v in (self.scope_id, self.scope_revision))):
            unavailable()
        # Lazy import: the policy module itself never constructs or migrates storage.
        from officer_document_ledger import money
        if money(self.usd_limit) < 1_110_000_000:
            unavailable()
        return self


def load_allowance(parent, *, enabled=False, demo=False, clock=time.time):
    """Read one fixed private file only after both explicit gates allow it.

    POSIX: exactly as before: the parent must be a private folder (0700, by lstat and on its opened descriptor), and
    the policy a private regular file (0600, one name) opened without following a link by the parent's descriptor,
    read once, unchanged meanwhile, still named by its name, with the parent unchanged. Windows: the same checks
    through the no-link walk: the parent is opened by handle with every folder above it (any reparse point refused)
    and its owner and access list checked; the policy is opened relative to it, with its owner, protected access
    list and single name checked on its handle, and identities read from handles (``file_identity``)."""
    if enabled is not True or demo is not False:
        return None
    if ps.WIN:
        if not ps.folder_walk_available():
            unavailable()
        current(clock)
        return _load_windows(parent, clock)
    if os.name != 'posix' or not hasattr(os,'O_NOFOLLOW'):
        unavailable()
    current(clock)
    fd = parent_fd = None
    try:
        # parent is the verified attachment state parent supplied by the host,
        # never an HTTP-selected path. Do not create a policy or state directory.
        private(os.lstat(parent), True)
        parent_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        parent_identity=private(os.fstat(parent_fd),True)[:2]
        fd = os.open(POLICY_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        before = os.fstat(fd)
        private(before)
        if not 1 <= before.st_size <= 4096:
            unavailable()
        raw = os.read(fd, 4097)
        if len(raw) != before.st_size or identity(before) != identity(os.fstat(fd)):
            unavailable()
        if identity(before) != identity(os.stat(POLICY_FILENAME, dir_fd=parent_fd, follow_symlinks=False)):
            unavailable()
        if private(os.lstat(parent),True)[:2] != parent_identity:
            unavailable()
        return _parse(raw, clock)
    except (OSError, ValueError, TypeError, RecursionError):
        unavailable()
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


def _parse(raw, clock):
    """The policy's closed JSON content, the same on every system."""
    def unique(items):
        out = {}
        for k, v in items:
            if k in out:
                unavailable()
            out[k] = v
        return out
    obj = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                     parse_constant=lambda _: unavailable())
    if (type(obj) is not dict or set(obj) != {'version','evidenceId','scopeId','scopeRevision','usdLimit'}
            or type(obj['version']) is not int or obj['version'] != 1):
        unavailable()
    return HostAllowance(obj['evidenceId'], obj['scopeId'], obj['scopeRevision'], obj['usdLimit']).validate(clock)


def _load_windows(parent, clock):
    """The POSIX checks, made through the walk and on handles (Windows' stat has no owner or mode, and its st_dev
    changed in Python 3.12, so identities come from ``private_storage.file_identity``)."""
    try:
        with ps.open_folder(str(parent), private=True) as folder:
            parent_identity = folder.identity()[:2]
            fd = folder.open(POLICY_FILENAME, single_link=True, private=True)
            try:
                before = tuple(ps.file_identity(fd))
                if not 1 <= before[2] <= 4096:
                    unavailable()
                raw = os.read(fd, 4097)
                if len(raw) != before[2] or tuple(ps.file_identity(fd)) != before:
                    unavailable()
            finally:
                os.close(fd)
            again = folder.open(POLICY_FILENAME, single_link=True, private=True)   # still the file that was read
            try:
                if tuple(ps.file_identity(again)) != before:
                    unavailable()
            finally:
                os.close(again)
        with ps.open_folder(str(parent), private=True) as folder:                   # and still the same folder
            if folder.identity()[:2] != parent_identity:
                unavailable()
        return _parse(raw, clock)
    except (OSError, ValueError, TypeError, RecursionError):
        unavailable()


def document_pricing():
    return _pricing_for(EVIDENCE_ID, PRICING_REVISION)


def _pricing_for(evidence_id, pricing_revision):
    from officer_document_ledger import DocumentPricing
    return DocumentPricing(pricing_revision, BILLING_REVISION, 'default_global_or_us',
                           *HIGH, qualification_id=evidence_id)


def predecessor_terms():
    """The exact (profile, pricing) each listed predecessor registered its budget with, for the predecessors whose
    recorded terms equal today's. Only the evidence id and pricing revision differ from this qualification's."""
    today = dict(profile_revision=PROFILE_REVISION, billing_revision=BILLING_REVISION, high=HIGH, low=LOW,
                 low_threshold=LOW_THRESHOLD, max_input=MAX_INPUT, per_call_usd=PER_CALL_USD)
    terms = []
    for entry in PREDECESSORS:
        rest = {k: v for k, v in entry.items() if k not in ('evidence_id', 'pricing_revision')}
        if rest == today and EVIDENCE_ID != entry['evidence_id'] and PRICING_REVISION != entry['pricing_revision']:
            terms.append((_profile_for(entry['evidence_id'], entry['pricing_revision']),
                          _pricing_for(entry['evidence_id'], entry['pricing_revision'])))
    return tuple(terms)


def validate_pricing(profile, pricing):
    if profile != input_profile() or pricing != document_pricing():
        unavailable()
