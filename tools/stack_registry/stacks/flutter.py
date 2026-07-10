"""Flutter stack patterns — Dart-specific cosmetic vs functional diff signals.

Extracted from `tools/cosmetic_classifier.py` circa V16.3 — the original home
of these regexes. Move here as part of H26 multi-stack dispatch so other
guardians (schema_drift, agent_verifier) can share the same "am I in a
Flutter repo" signal with the cosmetic classifier.
"""

COSMETIC_PATTERNS: list[str] = [
    r'#[0-9a-fA-F]{6,8}\b',               # hex colors
    r'\bColor\(0x[0-9a-fA-F]{8}\)',       # Dart Color(0xFF...)
    r'\brgba?\([0-9, .]+\)',              # rgb / rgba
    r'\bhsl[a]?\([0-9, %]+\)',            # hsl / hsla
    r'\bfontFamily:\s*',                  # font family
    r'\bfontSize:\s*\d',                  # font size
    r'\bfontWeight:\s*',                  # font weight
    r'\bletterSpacing:\s*',               # letter spacing
    r'\bEdgeInsets\.(only|all|symmetric|fromLTRB)',
    r'\bpadding(?:Left|Right|Top|Bottom)?:\s*',
    r'\bmargin(?:Left|Right|Top|Bottom)?:\s*',
    r'\bborderRadius:\s*',
    r'\bBorderRadius\.',
    r'\bDuration\(milliseconds:\s*\d',
    r'\bopacity:\s*',
]

FUNCTIONAL_PATTERNS: list[str] = [
    r'\b(if|else|switch|case)\b',
    r'\bawait\b',
    r'\b(async|Future|Stream)\b',
    r"""\.from\(["']""",
    r'\.select\(',
    r'\.rpc\(',
    r'\.eq\(',
    r'\.ilike\(',
    r'\.insert\(',
    r'\.update\(',
    r'\bthrow\b',
    r'\btry\b',
    r'\bcatch\b',
    r'\breturn\b\s+(?!null)',
]

TEST_PATH_MARKERS: list[str] = ["test/", "_test.dart"]
