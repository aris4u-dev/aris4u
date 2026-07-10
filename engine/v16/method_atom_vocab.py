"""Vocabulario controlado de los 'átomos de método' de ARIS4U.

Un átomo de método registra un problema cuantitativo o patrón de ingeniería
resuelto, indexado por la ESTRUCTURA del problema (no por su dominio de
aplicación) para detectar transferencia entre problemas estructuralmente
análogos (ej. teoría de colas aplica a ambulancias, citas y turnos por igual).

Tres ejes ORTOGONALES (decisión el usuario 2026-06-22, auditoría 87 agentes):
- ``problem_class``: estructura del problema del MUNDO (modelo cuantitativo).
- ``artifact_type``: patrón de la SOLUCIÓN de software (opcional; ``None`` en modelos puros).
- ``regime``: predictibilidad. ``pure-random`` NO se ingiere (sin método acumulable).

Nombres alineados a MSC2020 / INFORMS / GoF. Ver memoria project_atom_method_engine.
"""

# Eje 1 — estructura del problema del mundo (obligatorio en un átomo de método).
PROBLEM_CLASSES: frozenset[str] = frozenset({
    "newtonian-dynamics",        # fuerzas/energía/movimiento, F=ma (batería EV)
    "pde-simulation",            # campos continuos, integrar PDEs, caótico (clima, térmico)
    "queueing",                  # llegadas aleatorias + servidores + espera (ambulancias, citas)
    "stochastic-process",        # evolución aleatoria en el tiempo, Markov (fiabilidad)
    "time-series-forecasting",   # predicción con dependencia temporal (demanda)
    "supervised-learning",       # predecir target desde features etiquetados (clasificar leads)
    "unsupervised-learning",     # estructura sin etiquetas: clustering, anomalía (fraude)
    "mathematical-optimization", # min/max objetivo con restricciones (scheduling)
    "combinatorial-optimization",# cubrir/enumerar espacios discretos (elegibilidad, covering)
    "network-flow",              # grafos: caminos mínimos, flujos (ruta vial A→B)
    "probability-estimation",    # prob. condicional/exacta, Bayes (scoring de aprobación)
    "information-compression",   # minimizar representación sin perder señal (prompt, BPE)
    "embedding-retrieval",       # similitud vectorial, recall semántico (ARIS4U recall)
    "sequential-decision",       # decisiones en el tiempo, recompensa diferida (bandits, PID)
})

# Eje 2 — patrón de la solución de software (opcional).
ARTIFACT_TYPES: frozenset[str] = frozenset({
    "multi-tenant-isolation",    # aislar datos por tenant (RLS, namespacing)
    "access-control",            # RBAC / scoped / jerárquico
    "schema-migration",          # evolución de schema idempotente, rollback
    "event-driven-state-machine",# estados discretos + transiciones por evento
    "ledger-append-only",        # log inmutable; saldo = agregado (inventario, KZ points)
    "port-adapter-integration",  # interfaz estable + implementaciones MOCK/LIVE
    "pub-sub-realtime",          # propagación de estado a N suscriptores
    "document-workflow-pipeline",# OCR→extract→classify→human-review→archive
    "entity-resolution",         # record linkage sin clave común
    "coordination-protocol",     # saga, CQRS, circuit-breaker, consistencia eventual
    "corrective-action-loop",    # finding→plan→evidencia→cierre con trail
    "adversarial-review",        # fan-out de roles antagónicos → síntesis
    "idempotency-guard",         # dedup/exactly-once de efectos (UNIQUE+ON CONFLICT, webhooks)
})

# Eje 3 — predictibilidad. ``pure-random`` se rechaza en la ingesta.
REGIMES: frozenset[str] = frozenset({
    "deterministic",             # reglas fijas (software, cambio de código)
    "chaotic-deterministic",     # determinista pero sensible a c.i. (clima, Lorenz)
    "stochastic",                # aleatorio con estructura (demanda, colas)
    "pure-random",               # entropía máxima, incompresible (lotería justa) → NO ingerir
})

# Régimen que el sistema se niega a indexar: incompresible = sin método acumulable.
UNMODELABLE_REGIME: str = "pure-random"

# Eje 4 — ADOPCIÓN: ¿el cliente/proyecto usa este método, y qué tan bien? (revelado por
# un catálogo real: separa "qué existe" de "qué se usa"). 'gap-no-method-exists' = gap
# de investigación real (no hay método conocido), distinto de 'unused' (existe, no se usa).
ADOPTION_STATES: frozenset[str] = frozenset({
    "used",                 # implementado y en uso
    "used-naive",           # en uso pero de forma débil; hay método mejor
    "unused",               # existe y aplica a esta clase, NO se usa (el espacio no-explorado)
    "gap-no-method-exists", # problema sin método conocido (gap de investigación, raro)
})

# Eje 5 — EVIDENCIA: ¿de dónde sale la confianza en el átomo?
EVIDENCE_KINDS: frozenset[str] = frozenset({
    "calibrated",  # parámetros medidos en un runtime real (átomo-de-implementación)
    "catalog",     # conocimiento puro verificado del estado del arte, sin medir aquí
})


def _validate_vocab(
    problem_class: str | None,
    artifact_type: str | None,
    regime: str | None,
    adoption: str | None = None,
    evidence: str | None = None,
) -> str | None:
    """Comprueba que cada eje provisto pertenezca a su vocabulario controlado.

    Args:
        problem_class: Eje 1 (estructura del problema), o ``None``.
        artifact_type: Eje 2 (patrón de software), o ``None``.
        regime: Eje 3 (predictibilidad), o ``None``.
        adoption: Eje 4 (estado de adopción), o ``None``.
        evidence: Eje 5 (origen de la evidencia), o ``None``.

    Returns:
        Mensaje de error del primer eje inválido, o ``None`` si todos son válidos.
    """
    checks = (
        (problem_class, PROBLEM_CLASSES, "problem_class"),
        (artifact_type, ARTIFACT_TYPES, "artifact_type"),
        (regime, REGIMES, "regime"),
        (adoption, ADOPTION_STATES, "adoption"),
        (evidence, EVIDENCE_KINDS, "evidence"),
    )
    for value, vocab, name in checks:
        if value is not None and value not in vocab:
            return (
                f"{name} '{value}' no está en el vocabulario. "
                f"Valores válidos: {', '.join(sorted(vocab))}"
            )
    return None


def validate_method_atom(
    problem_class: str | None,
    artifact_type: str | None,
    regime: str | None,
    validity_domain: str | None,
    adoption: str | None = None,
    evidence: str | None = None,
) -> tuple[bool, str | None]:
    """Valida los ejes de un átomo de método antes de persistirlo.

    Un item es átomo de método si trae ``problem_class`` O ``artifact_type`` — los
    átomos puro-software (patrón sin modelo del mundo) son válidos sin problem_class
    (un caso real lo evidenció: las apps transaccionales son artifact-heavy). En ese caso se
    exigen ``regime`` y ``validity_domain`` (sin "dónde rompe" el átomo se vuelve techo
    en vez de piso). ``regime='pure-random'`` se rechaza siempre.

    Args:
        problem_class: Eje 1 (estructura del problema), opcional si hay artifact_type.
        artifact_type: Eje 2 (patrón de software), opcional si hay problem_class.
        regime: Eje 3 (predictibilidad).
        validity_domain: Texto que describe dónde aplica y dónde rompe el esqueleto.

    Returns:
        ``(ok, error)``. ``ok`` es ``True`` si pasa; en caso contrario ``error``
        explica el motivo del rechazo.
    """
    vocab_error = _validate_vocab(problem_class, artifact_type, regime, adoption, evidence)
    if vocab_error is not None:
        return False, vocab_error

    # Regla de ingesta: lo incompresible no se acumula (comprimir = predecir).
    if regime == UNMODELABLE_REGIME:
        return False, (
            "regime='pure-random' no se ingiere: un sistema de entropía máxima "
            "(p.ej. lotería justa) no tiene método acumulable. Es incompresible "
            "y por tanto impredecible — no hay esqueleto que transferir."
        )

    # Disciplina piso-no-techo: un átomo (problem_class O artifact_type) necesita
    # régimen y dominio de validez para aplicarse con seguridad solo en su zona.
    is_atom = problem_class is not None or artifact_type is not None
    if is_atom:
        if not regime:
            return False, "Un átomo de método (problem_class o artifact_type) requiere 'regime'."
        if not (validity_domain and validity_domain.strip()):
            return False, (
                "Un átomo de método requiere 'validity_domain' (dónde aplica y dónde "
                "rompe). Sin él el átomo se vuelve techo en vez de piso."
            )

    return True, None


def structural_signature(
    problem_class: str | None,
    artifact_type: str | None,
    regime: str | None,
) -> str:
    """Firma estructural para agrupar átomos candidatos a duplicado.

    Dos átomos con la misma firma comparten estructura (misma clase/patrón/régimen) y
    son candidatos a dedup — probablemente instancias del mismo método canónico (caso real:
    6 ledgers y 12 FSM colapsan por firma). NO es prueba de identidad: el veredicto
    canónico-vs-instancia lo da el humano/Claude al ver la colisión (el repo PROPONE,
    Claude DISPONE).

    Args:
        problem_class: Eje 1 o ``None``.
        artifact_type: Eje 2 o ``None``.
        regime: Eje 3 o ``None``.

    Returns:
        Firma ``"problem_class|artifact_type|regime"`` con ``'-'`` para los ausentes.
    """
    return "|".join(v or "-" for v in (problem_class, artifact_type, regime))
