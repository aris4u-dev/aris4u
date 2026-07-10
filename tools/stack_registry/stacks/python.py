"""Python stack patterns.

Cosmetic is rare in pure Python; patterns here target frontends built
with Python (Jinja2 templates, Django templates) + visualisation libs
(Matplotlib, Plotly) where styling may appear.
"""

COSMETIC_PATTERNS: list[str] = [
    r'#[0-9a-fA-F]{6,8}\b',               # hex colors (templates, plots)
    r'\brgba?\(',
    r'plt\.set_(?:title|xlabel|ylabel|facecolor)',  # matplotlib styling
    r'\bfigsize=',
    r'color\s*=\s*["\']#?[0-9a-fA-F]+',
    r'\bfontsize\s*=',
    r'\bstyle=',
]

FUNCTIONAL_PATTERNS: list[str] = [
    r'\b(if|elif|else)\b',
    r'\bfor\s+\w+\s+in\b',
    r'\bwhile\b',
    r'\bdef\s+\w+\(',
    r'\b(async\s+def|await)\b',
    r'\b(try|except|finally|raise)\b',
    r'\bimport\s+',
    r'\bfrom\s+\S+\s+import\b',
    r'\bclass\s+\w+',
    r'\breturn\s+(?!None)',
    r'\bwith\s+',
]

TEST_PATH_MARKERS: list[str] = ["tests/", "test_", "_test.py"]
