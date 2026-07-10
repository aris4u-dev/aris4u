"""Node / TypeScript stack patterns (CSS-in-JS, JSX, Tailwind).

Covers React, Vue, Svelte, Astro when detected via package.json.
"""

COSMETIC_PATTERNS: list[str] = [
    r'#[0-9a-fA-F]{6,8}\b',               # hex colors
    r'\brgba?\([0-9, .]+\)',              # rgb / rgba
    r'\bhsl[a]?\([0-9, %]+\)',            # hsl / hsla
    r'\bfont(?:Family|Size|Weight):\s*',  # CSS-in-JS font
    r'\b(?:padding|margin)(?:Left|Right|Top|Bottom)?:\s*',
    r'\bborderRadius:\s*',
    r'\bletterSpacing:\s*',
    r'\bopacity:\s*',
    r'className="[^"]*(?:bg-|text-|p-|m-|rounded|shadow|font-)',  # Tailwind
    r'\bstyled\.',                         # styled-components
    r'\bcss`',                             # emotion / styled css``
]

FUNCTIONAL_PATTERNS: list[str] = [
    r'\b(if|else|switch|case)\b',
    r'\bawait\b',
    r'\b(async|Promise|Observable)\b',
    r'\btry\b',
    r'\bcatch\b',
    r'\bthrow\b',
    r'\bimport\s+',
    r'\bexport\s+',
    r'\bfunction\s+\w+',
    r'\breturn\s+(?!null)',
    r'\.fetch\(',
    r'\baxios\.',
]

TEST_PATH_MARKERS: list[str] = [
    "__tests__/", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx",
    ".test.js", ".spec.js",
]
