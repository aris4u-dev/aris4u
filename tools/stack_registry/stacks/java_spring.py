"""Java / Kotlin / Spring Boot stack patterns.

Cosmetic in JVM land tends to live in templates (Thymeleaf, JSP) and
resource bundles, not .java itself. This set captures the common leak
points — if a Java/Spring project adds visual tweaks, these patterns
will fire.
"""

COSMETIC_PATTERNS: list[str] = [
    r'#[0-9a-fA-F]{6,8}\b',               # hex colors (Thymeleaf, CSS)
    r'\brgba?\([0-9, .]+\)',
    r'\bColor\.(?:decode|parseColor)\(',
    r'\bcolor:\s*',                        # CSS in .properties / templates
    r'\bpadding:\s*',
    r'\bmargin:\s*',
    r'\bfont-(?:family|size|weight):\s*',
    r'\bborder-radius:\s*',
    r'@ColorRes\b',                        # Android-style resource
    r'@DrawableRes\b',
]

FUNCTIONAL_PATTERNS: list[str] = [
    r'\b(if|else|switch|case)\b',
    r'\b(public|private|protected)\s+(?:static\s+)?\w+\s+\w+\s*\(',
    r'\b(try|catch|finally|throw)\b',
    r'\bnew\s+\w+\(',
    r'@(?:RestController|Controller|Service|Repository|Autowired|Transactional|GetMapping|PostMapping|PutMapping|DeleteMapping)\b',
    r'\bAsync<',
    r'\bCompletableFuture',
    r'\breturn\s+(?!null)',
]

TEST_PATH_MARKERS: list[str] = [
    "/src/test/", "Test.java", "Test.kt", "Tests.java", "Tests.kt",
    "IT.java", "IntegrationTest",
]
