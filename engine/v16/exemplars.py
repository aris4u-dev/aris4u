"""
Exemplar queries for embedding-based intent classification.

180 exemplar queries across 5 intent categories (simple/fix/decision/implementation/research)
with balanced bilingual ES/EN coverage. Used as training data for nearest-neighbor classifier.
Counts vary per intent (30-45). `implementation` is over-represented with Spanish confirmation
phrases ("sigue con eso", "adelante") that are ambiguous — see M12_exemplars/README.md §Riesgos.

The embeddings are language-agnostic: "dame la lista" ≈ "show me the list" in embedding space.
"""

EXEMPLARS: dict[str, list[str]] = {
    "simple": [
        # English (15)
        "what is a tensor",
        "show me the list of modules",
        "list all pending issues",
        "what does V16 mean",
        "tell me about the hook system",
        "display current configuration",
        "what is the depth protocol",
        "list available commands",
        "show architecture overview",
        "what are the test results",
        "how many tests are there",
        "give me a summary",
        "show me the status",
        "what is ARIS",
        "what is the system",
        # Spanish (15)
        "qué es un tensor",
        "muéstrame la lista de módulos",
        "lista todos los problemas pendientes",
        "qué significa V16",
        "cuéntame del sistema de hooks",
        "muestra la configuración actual",
        "qué es el protocolo de profundidad",
        "lista los comandos disponibles",
        "muestra el resumen de la arquitectura",
        "cuáles son los resultados de los tests",
        "cuántos tests hay",
        "dame un resumen",
        "muestra el status",
        "qué es ARIS",
        "qué es el sistema",
        "no entiendo",
        "vuelve al modo anterior",
        "regresa al plan",
        "ok entendido",
        "listo",
    ],
    "fix": [
        # English (15)
        "fix the bug in the classifier",
        "the hook is broken",
        "X is not working properly",
        "session manager is crashing",
        "database lock timeout error",
        "regex patterns are failing",
        "token estimation is wrong",
        "embedding service is down",
        "HTTP 500 on session endpoint",
        "the logger is corrupted",
        "X is not working",
        "there is an error in X",
        "why does X fail",
        "X throws an exception",
        "the server went down",
        # Spanish (15)
        "arregla el bug en el clasificador",
        "el hook está roto",
        "X no funciona correctamente",
        "el gestor de sesiones se cae",
        "error de timeout en la base de datos",
        "los patrones regex están fallando",
        "la estimación de tokens es incorrecta",
        "el servicio de embeddings está caído",
        "HTTP 500 en el endpoint de sesión",
        "el logger está corrompido",
        "X no jala",
        "se cayó todo",
        "hay un error en X",
        "por qué falla X",
        "X tira excepción",
    ],
    "decision": [
        # English (15)
        "should we use Ollama or Claude API",
        "which architecture is better for this",
        "compare SQLite vs PostgreSQL",
        "should we adopt the cascade classifier",
        "what depth level is recommended",
        "is embedding-based classification the right approach",
        "which model should we deploy",
        "should we use Redis or in-memory cache",
        "what's the best testing strategy",
        "should we migrate to a new framework",
        "should we use embeddings or regex",
        "should we change the approach",
        "is it worth investing in this",
        "what's the best option",
        "should we switch to a new framework",
        # Spanish (15)
        "debemos usar Ollama o Claude API",
        "cuál arquitectura es mejor para esto",
        "compara SQLite vs PostgreSQL",
        "debemos adoptar el clasificador en cascada",
        "qué nivel de profundidad se recomienda",
        "¿la clasificación basada en embeddings es el enfoque correcto?",
        "cuál modelo deberíamos desplegar",
        "debemos usar Redis o caché en memoria",
        "cuál es la mejor estrategia de testing",
        "debemos migrar a un nuevo framework",
        "debemos usar embeddings o regex",
        "deberíamos cambiar el enfoque",
        "vale la pena invertir en esto",
        "cuál es la mejor opción",
        "conviene cambiar a un nuevo framework",
        "ver si funciono lo que hicimos",
        "verificar si el cambio sirvió",
        "checar si V16 funciona bien",
        "evaluar si el approach es correcto",
        "comprobar si los resultados son buenos",
    ],
    "implementation": [
        # English (15)
        "build me a classifier using embeddings",
        "create a new monitoring dashboard",
        "implement the cascade routing system",
        "add support for multi-language queries",
        "write a test suite for the engine",
        "develop the adaptive depth module",
        "construct the security audit framework",
        "set up automated deployment pipeline",
        "make a performance optimization layer",
        "build the knowledge base indexer",
        "build the login module complete",
        "go ahead and build it",
        "add the feature for X",
        "implement the X module",
        "develop the API for X",
        # Spanish (15)
        "construye un clasificador usando embeddings",
        "crea un nuevo panel de monitoreo",
        "implementa el sistema de enrutamiento en cascada",
        "agrega soporte para queries multiidioma",
        "escribe un suite de tests para el motor",
        "desarrolla el módulo de profundidad adaptativa",
        "construye el framework de auditoría de seguridad",
        "configura la tubería de despliegue automatizado",
        "haz una capa de optimización de rendimiento",
        "construye el indexador de base de conocimientos",
        "construye el módulo de login completo",
        "adelante hazlo",
        "agrega la funcionalidad de X",
        "implementa el módulo de X",
        "desarrolla el API de X",
        "sigue con eso",
        "sigue adelante",
        "lanza eso",
        "pon más agentes a trabajar",
        "continúa sin parar",
        "sigue con las correcciones",
        "adelante con todo",
        "keep going",
        "launch it",
        "continue with that",
        "construye el módulo de autenticación completo",
        "crea el sistema de usuarios",
        "desarrolla la funcionalidad completa de pagos",
        "construye todo el backend",
        "haz el módulo de notificaciones",
    ],
    "research": [
        # English (15)
        "investigate how embedding similarity works",
        "research the latest classification techniques",
        "analyze the performance benchmarks",
        "explore distributed tracing patterns",
        "study the existing token accounting methods",
        "examine the cascade routing literature",
        "investigate alternative embedding models",
        "research semantic similarity metrics",
        "analyze caching strategies for embeddings",
        "study multi-tier classification systems",
        "investigate the best practices",
        "research alternatives to X",
        "find alternatives to X",
        "analyze the options",
        "evaluate X in depth",
        # Spanish (15)
        "investiga cómo funciona la similitud de embeddings",
        "researcha las técnicas de clasificación más recientes",
        "analiza los benchmarks de rendimiento",
        "explora patrones de rastreo distribuido",
        "estudia los métodos existentes de contabilidad de tokens",
        "examina la literatura del enrutamiento en cascada",
        "investiga modelos alternativos de embeddings",
        "researcha métricas de similitud semántica",
        "analiza estrategias de caché para embeddings",
        "estudia sistemas de clasificación de múltiples niveles",
        "investiga sobre las mejores prácticas",
        "busca alternativas a X",
        "encuentra alternativas a X",
        "analiza las opciones",
        "evalúa X en profundidad",
    ],
}


def get_all_exemplars() -> dict[str, list[str]]:
    """
    Returns the complete exemplar dataset.

    Returns:
        dict[str, list[str]]: Dictionary mapping intent → list of exemplar queries.
    """
    return EXEMPLARS


def get_exemplars_for_intent(intent: str) -> list[str]:
    """
    Get exemplars for a specific intent.

    Args:
        intent: One of 'simple', 'fix', 'decision', 'implementation', 'research'.

    Returns:
        list[str]: List of exemplar queries for that intent (30 per intent).

    Raises:
        ValueError: If intent is not recognized.
    """
    if intent not in EXEMPLARS:
        raise ValueError(f"Unknown intent: {intent}. Must be one of {list(EXEMPLARS.keys())}")
    return EXEMPLARS[intent]


def intent_names() -> list[str]:
    """
    Returns the list of all intent names.

    Returns:
        list[str]: ['simple', 'fix', 'decision', 'implementation', 'research']
    """
    return list(EXEMPLARS.keys())
