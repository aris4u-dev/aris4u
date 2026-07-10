"""Generic fallback — union of all stack patterns for unknown repos.

When `detect_stack` can't identify the project (no marker files, no
recognized extension), the classifier still needs something to work
with. This module aggregates the broadest set of cosmetic and
functional signals so the cosmetic ratio remains meaningful.
"""

# Intentionally duplicated from the per-stack modules so generic is
# self-contained (avoids circular imports). Keep in sync manually.

COSMETIC_PATTERNS: list[str] = [
    # Colors
    r'#[0-9a-fA-F]{6,8}\b',
    r'\bColor\(0x[0-9a-fA-F]{8}\)',
    r'\brgba?\([0-9, .]+\)',
    r'\bhsl[a]?\([0-9, %]+\)',
    # Typography
    r'\bfontFamily:\s*',
    r'\bfontSize:\s*\d',
    r'\bfontWeight:\s*',
    r'\bletterSpacing:\s*',
    r'\bfont-(?:family|size|weight):\s*',
    # Spacing
    r'\bEdgeInsets\.(only|all|symmetric|fromLTRB)',
    r'\bpadding(?:Left|Right|Top|Bottom)?:\s*',
    r'\bmargin(?:Left|Right|Top|Bottom)?:\s*',
    # Shape / visual
    r'\bborderRadius:\s*',
    r'\bBorderRadius\.',
    r'\bborder-radius:\s*',
    r'\bopacity:\s*',
    r'\bDuration\(milliseconds:\s*\d',
    # Framework-specific
    r'className="[^"]*(?:bg-|text-|p-|m-|rounded|shadow|font-)',
    r'\bstyled\.',
    r'\bcss`',
]

FUNCTIONAL_PATTERNS: list[str] = [
    r'\b(if|else|switch|case|elif)\b',
    r'\bawait\b',
    r'\b(async|Future|Stream|Promise|Observable)\b',
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
    r'\breturn\b\s+(?!null|None)',
    r'\bimport\s+',
    r'\bexport\s+',
    r'\bdef\s+\w+\(',
    r'@(?:RestController|Service|Autowired)\b',
]
