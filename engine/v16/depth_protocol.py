import re
import logging
from .config import DEPTH_LEVELS

logger = logging.getLogger(__name__)

# Keep regex patterns as fallback (graceful degradation if Ollama is down)
IMPL_PATTERNS = re.compile(
    r"\b(build|create|implement|develop|write|code|add feature|new module|set up|configure|deploy|install|construct|make"
    r"|construye|crea|implementa|desarrolla|escribe|programa|agrega|configura|despliega|instala|monta|arma|ejecuta|completa|realiza|haz|hazlo|lanza)\b",
    re.IGNORECASE,
)
DECISION_PATTERNS = re.compile(
    r"\b(should|which|best|recommend|compare|evaluate|choose|decide|option|pick|select|prefer|versus|vs"
    r"|debemos|deberia|cual|mejor|recomienda|compara|evalua|elige|decide|opcion|selecciona|prefiere|conviene|vale la pena)\b",
    re.IGNORECASE,
)
FIX_PATTERNS = re.compile(
    r"\b(fix|bug|error|broken|not working|fails|crash|issue|debug|repair|patch|resolve|troubleshoot|500|404|503|timeout|exception|traceback|stacktrace"
    r"|arregla|corrige|repara|roto|no funciona|falla|problema|soluciona|resuelve|rompe|cayo|caido|murio)\b",
    re.IGNORECASE,
)
RESEARCH_PATTERNS = re.compile(
    r"\b(research|investigate|analyze|explore|find|search|look into|deep dive|audit|review|check"
    r"|investiga|analiza|explora|busca|profundiza|audita|revisa|verifica|examina|estudia|evalua|diagnostica|escanea|analisis|evaluacion)\b",
    re.IGNORECASE,
)


def _classify_regex(query: str) -> str:
    """
    Fallback regex-based classifier (legacy, 93% wrong on real data).

    Used only when embedding classifier (f1_classifier.classify_v16) raises
    because Ollama is unreachable. NOT invoked for low-confidence cases —
    those fall to "simple" inside classify_v16.

    Evaluation order: implementation → decision → fix → research → simple.
    First pattern match wins; no ambiguity resolution.

    Args:
        query: User query string

    Returns:
        Intent: 'simple', 'fix', 'decision', 'implementation', or 'research'
    """
    if IMPL_PATTERNS.search(query):
        return "implementation"
    if DECISION_PATTERNS.search(query):
        return "decision"
    if FIX_PATTERNS.search(query):
        return "fix"
    if RESEARCH_PATTERNS.search(query):
        return "research"
    return "simple"


def classify(query: str) -> str:
    """
    Classify a query intent using embedding-based nearest-neighbor (V16 F1.PERCEPCION).

    Replaces the broken regex-based classifier with embedding similarity matching.
    Falls back to regex if the embedding classifier is uncertain or Ollama is unreachable.

    Args:
        query: User query string (ES or EN)

    Returns:
        Intent: one of 'simple', 'fix', 'decision', 'implementation', 'research'

    Implementation:
    - Primary: Embedding nearest-neighbor via f1_classifier.classify_v16()
    - Fallback: Regex patterns if Ollama unreachable or confidence too low
    """
    try:
        from .f1_classifier import classify_v16

        return classify_v16(query)
    except Exception as e:
        logger.warning(f"Embedding classifier failed, falling back to regex: {e}")
        return _classify_regex(query)


def get_levels(query_type: str) -> list[int]:
    return DEPTH_LEVELS.get(query_type, [1])


LEVEL_NAMES = {
    1: "RECALL",
    2: "RESEARCH",
    3: "ANALYZE",
    4: "COMPARE",
    5: "VERIFY",
    6: "SYNTHESIZE",
    7: "IMPLEMENT",
    8: "REVIEW",
    9: "TEST",
    10: "CAPTURE",
}

DIRECTIVES = {
    "simple": "Check RECALL results. Answer directly.",
    "fix": (
        "DEPTH PROTOCOL (fix):\n"
        "1. RECALL: Check prior decisions/guards on this topic\n"
        "5. VERIFY: Reproduce the bug, confirm root cause\n"
        "7. IMPLEMENT: Fix completely\n"
        "9. TEST: Verify fix works, no regressions"
    ),
    "decision": (
        "DEPTH PROTOCOL (decision):\n"
        "1. RECALL: Check if this was already decided\n"
        "2. RESEARCH: Search web + GitHub for latest solutions\n"
        "3. ANALYZE: Present 3+ alternatives with data\n"
        "4. COMPARE: Best option with benchmarks, not opinions\n"
        "5. VERIFY: Confirm recommendation exists and works\n"
        "6. SYNTHESIZE: Fundamented recommendation with evidence"
    ),
    "research": (
        "DEPTH PROTOCOL (research):\n"
        "1. RECALL: Check if this was already researched\n"
        "2. RESEARCH: 10+ SPECIFIC search queries, not generic\n"
        "3. ANALYZE: Present ALL findings with sources\n"
        "4. COMPARE: Compare alternatives with data\n"
        "5. VERIFY: Verify every source URL exists\n"
        "6. SYNTHESIZE: Complete synthesis with evidence"
    ),
    "implementation": (
        "FULL DEPTH PROTOCOL:\n"
        "1. RECALL: Check prior decisions/guards on this topic\n"
        "2. RESEARCH: Search web + GitHub for latest approaches\n"
        "3. ANALYZE: 3+ alternatives minimum\n"
        "4. COMPARE: Best option with benchmarks\n"
        "5. VERIFY: Confirm it actually works\n"
        "6. SYNTHESIZE: Fundamented recommendation\n"
        "7. IMPLEMENT: Build complete — not skeleton\n"
        "8. REVIEW: Consider security + edge cases\n"
        "9. TEST: Write and run tests\n"
        "10. CAPTURE: Note decisions for future sessions"
    ),
}


