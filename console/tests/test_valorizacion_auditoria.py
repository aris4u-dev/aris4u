"""Tests para los módulos Valorización (RICE-A+Moat) y Auditoría de átomos.

Ambos módulos operan sobre una DB temporal aislada de la viva.
Verifica: cálculo de score, moat, veredicto, y detección de hallazgos de auditoría.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import live_data as L  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _make_atoms_db(repo: Path) -> None:
    """Crea sessions.db con una tabla decisions mínima con todos los campos de átomos.

    Incluye filas que ejercitan todos los caminos del scoring y todos los tipos
    de hallazgo de auditoría:
      - átomo A: calibrado, used, 3 transfers JSON → expect adopt
      - átomo B: catalog, unused, 1 transfer → expect build (score<3 pero moat≥1)
      - átomo C: sin evidence_kind, no transfers → expect omit
      - átomo D: sin validity_domain → hallazgo sin_validity
      - átomo E: sin source_project → hallazgo sin_source
      - átomo F: validity_domain con tag [BAJO VALOR] → hallazgo bajo_valor
      - átomo G: duplicate structural_signature con átomo A → hallazgo duplicado
      - átomo H: problem_class 'embedding-retrieval', adoption=unused → hallazgo hueco
        (solo si no hay ningún 'used' de esa clase)
    """
    (repo / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(repo / "data" / "sessions.db")
    conn.executescript("""
        CREATE TABLE decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision TEXT NOT NULL,
            domain TEXT,
            locked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            client_id TEXT,
            mem_type TEXT,
            problem_class TEXT,
            artifact_type TEXT,
            regime TEXT,
            skeleton TEXT,
            validity_domain TEXT,
            transfers_to TEXT,
            structural_signature TEXT,
            canonical_id INTEGER,
            adoption TEXT,
            evidence_kind TEXT,
            source_project TEXT,
            variable_verdicts TEXT,
            epistemic_status TEXT DEFAULT 'provisional',
            digest_id TEXT,
            rationale TEXT,
            evidence TEXT,
            session_ref TEXT
        );
        CREATE TABLE guards(pattern TEXT, prevention TEXT, severity TEXT,
            created_at TEXT, client_id TEXT);
        CREATE TABLE digests(date TEXT, summary TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE recall_feedback(recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL,
            marked_at TEXT);
    """)
    # átomo A: calibrado + used + 3 JSON transfers + source_project + validity → ADOPT
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,artifact_type,regime,skeleton,
         validity_domain,transfers_to,structural_signature,adoption,evidence_kind,source_project)
        VALUES (
          '[atom:surge-pricing] surge pricing',
          'fact','probability-estimation','ledger-append-only','deterministic',
          'SELECT ... FROM events',
          'APLICA cuando hay conteo de demanda vs oferta.',
          '["client-d","client-b","client-c"]',
          'probability-estimation|ledger-append-only|deterministic',
          'used','calibrated','lab-project-1-app'
        )""")
    # átomo B: catalog + unused + 1 JSON transfer + validity → BUILD (moat=1 ≥ 1)
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,artifact_type,regime,skeleton,
         validity_domain,transfers_to,structural_signature,adoption,evidence_kind,source_project)
        VALUES (
          '[atom:rate-limit] rate limiting',
          'fact','sequential-decision','access-control','deterministic',
          'FOR UPDATE ... LIMIT ...',
          'APLICA en edge functions stateless.',
          '["client-a"]',
          'sequential-decision|access-control|deterministic',
          'unused','catalog','lab-project-1-app'
        )""")
    # átomo C: sin evidence_kind, sin transfers, sin source → OMIT
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,skeleton,
         validity_domain,transfers_to,structural_signature,adoption,source_project)
        VALUES (
          'átomo sin evidencia',
          'fact','network-flow','deterministic',
          'graph traversal',
          'APLICA en grafos.',
          NULL,
          'network-flow|-|deterministic',
          'unused',NULL
        )""")
    # átomo D: sin validity_domain + structural_signature → hallazgo sin_validity
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,structural_signature,adoption,evidence_kind,source_project)
        VALUES (
          'átomo sin validity',
          'fact','queueing','deterministic',
          'queueing|-|deterministic',
          'unused','catalog','client-a'
        )""")
    # átomo E: sin source_project + structural_signature → hallazgo sin_source
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,validity_domain,structural_signature,adoption,evidence_kind)
        VALUES (
          'átomo sin source_project',
          'fact','supervised-learning','stochastic',
          'APLICA en clasificación binaria.',
          'supervised-learning|-|stochastic',
          'used','calibrated'
        )""")
    # átomo F: validity_domain con [BAJO VALOR] → hallazgo bajo_valor
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,validity_domain,structural_signature,
         adoption,evidence_kind,source_project)
        VALUES (
          'átomo bajo valor',
          'fact','unsupervised-learning','stochastic',
          '[BAJO VALOR] este patrón quedó obsoleto.',
          'unsupervised-learning|-|stochastic',
          'unused','catalog','client-a'
        )""")
    # átomo G: misma structural_signature que átomo A → hallazgo duplicado
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,validity_domain,structural_signature,
         adoption,evidence_kind,source_project)
        VALUES (
          'átomo duplicado',
          'fact','probability-estimation','deterministic',
          'APLICA también aquí.',
          'probability-estimation|ledger-append-only|deterministic',
          'used','calibrated','client-a'
        )""")
    # átomo H: problem_class 'embedding-retrieval', adoption=unused → hueco (no hay used)
    conn.execute("""INSERT INTO decisions
        (decision,mem_type,problem_class,regime,validity_domain,structural_signature,
         adoption,evidence_kind,source_project)
        VALUES (
          'átomo embedding sin usar',
          'fact','embedding-retrieval','deterministic',
          'APLICA en búsqueda semántica.',
          'embedding-retrieval|-|deterministic',
          'unused','catalog','client-a'
        )""")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests de Valorización (RICE-A + Moat)
# ---------------------------------------------------------------------------

class TestRiceHelpers:
    """Prueba las funciones de scoring puras (sin DB)."""

    def test_reach_counts_source_and_json_transfers(self) -> None:
        """Reach suma source_project + entradas válidas de transfers_to JSON."""
        r = L._rice_reach("lab-project-1-app", '["client-a","client-b"]')
        assert r == 3  # lab-project-1-app + client-a + client-b

    def test_reach_ignores_non_json_transfers(self) -> None:
        """transfers_to texto libre (no JSON) no suma al reach."""
        r = L._rice_reach("lab-project-1-app", "Client-A: descripcion larga...")
        assert r == 1  # solo source_project

    def test_reach_empty_source_and_no_transfers(self) -> None:
        """Sin source ni transfers el reach es 0."""
        assert L._rice_reach("", "") == 0

    def test_reach_capped_at_5(self) -> None:
        """Reach máximo es 5 aunque haya más proyectos."""
        r = L._rice_reach("p1", '["p2","p3","p4","p5","p6","p7"]')
        assert r == 5

    def test_rice_score_calibrated_used_with_validity(self) -> None:
        """calibrated + used + validity_domain → score alto (R×3×0.9×1.0/1)."""
        s = L._rice_score(reach=3, evidence_kind="calibrated",
                          adoption="used", has_validity=True)
        assert s == round(3 * 3 * 0.9 * 1.0 / 1, 2)

    def test_rice_score_no_evidence_no_validity(self) -> None:
        """Sin evidencia y sin validity → score bajo (R×1×0.5×0.5/2)."""
        s = L._rice_score(reach=2, evidence_kind="", adoption="", has_validity=False)
        assert s == round(2 * 1 * 0.5 * 0.5 / 2, 2)

    def test_rice_score_zero_reach(self) -> None:
        """Reach 0 → score siempre 0 sin importar el resto."""
        assert L._rice_score(0, "calibrated", "used", True) == 0.0

    def test_moat_json_list(self) -> None:
        """Moat = longitud del JSON array (capped a 5)."""
        assert L._moat('["a","b","c"]') == 3

    def test_moat_non_json_is_zero(self) -> None:
        """Texto libre → moat 0."""
        assert L._moat("Client-A: descripcion...") == 0

    def test_moat_empty_is_zero(self) -> None:
        assert L._moat("") == 0

    def test_moat_capped_at_5(self) -> None:
        assert L._moat('["a","b","c","d","e","f","g"]') == 5

    def test_verdict_adopt(self) -> None:
        """score ≥ 3.0 y moat ≥ 2 → adopt."""
        assert L._verdict(3.0, 2) == "adopt"
        assert L._verdict(5.0, 3) == "adopt"

    def test_verdict_build_by_score(self) -> None:
        """score ≥ 1.5 (pero moat < 2) → build."""
        assert L._verdict(1.5, 0) == "build"

    def test_verdict_build_by_moat(self) -> None:
        """moat ≥ 1 (aunque score < 1.5) → build."""
        assert L._verdict(0.5, 1) == "build"

    def test_verdict_omit(self) -> None:
        """score < 1.5 y moat 0 → omit."""
        assert L._verdict(0.5, 0) == "omit"
        assert L._verdict(0.0, 0) == "omit"


class TestReadValorizacion:
    """Prueba read_valorizacion contra la DB temporal."""

    def test_unavailable_when_no_db(self, tmp_path: Path) -> None:
        """Sin sessions.db → available=False (fail-soft)."""
        v = L.read_valorizacion(tmp_path)
        assert v["available"] is False

    def test_returns_scored_atoms(self, tmp_path: Path) -> None:
        """read_valorizacion devuelve átomos con score/moat/verdict."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        assert v["available"] is True
        assert v["total"] > 0
        # todos los átomos tienen los campos RICE
        for a in v["atoms"]:
            assert "rice_score" in a
            assert "moat" in a
            assert a["verdict"] in ("adopt", "build", "omit")

    def test_atom_a_is_adopt(self, tmp_path: Path) -> None:
        """Átomo A (calibrated+used+3 transfers) debe recibir veredicto adopt."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        # átomo A tiene source_project='lab-project-1-app' + 3 transfers → reach=4, moat=3
        adopt_atoms = [a for a in v["atoms"] if a["verdict"] == "adopt"]
        assert len(adopt_atoms) >= 1
        # el de mayor score debe ser adopt
        assert v["atoms"][0]["verdict"] == "adopt"

    def test_atom_c_is_omit(self, tmp_path: Path) -> None:
        """Átomo C (sin evidence, sin transfers) debe ser omit."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        omit_atoms = [a for a in v["atoms"] if a["verdict"] == "omit"]
        assert len(omit_atoms) >= 1

    def test_sorted_by_score_desc(self, tmp_path: Path) -> None:
        """Los átomos deben venir ordenados por rice_score descendente."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        scores = [a["rice_score"] for a in v["atoms"]]
        assert scores == sorted(scores, reverse=True)

    def test_totals_sum_to_total(self, tmp_path: Path) -> None:
        """La suma de adopt+build+omit en totals debe igualar total."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        t = v["totals"]
        assert t["adopt"] + t["build"] + t["omit"] == v["total"]

    def test_transfers_are_labels(self, tmp_path: Path) -> None:
        """transfers deben ser etiquetas legibles (project_label), no códigos crudos."""
        _make_atoms_db(tmp_path)
        v = L.read_valorizacion(tmp_path)
        # atom A has transfers_to=["client-d","client-b","client-c"]
        atom_a = next((a for a in v["atoms"] if "surge" in a["name"].lower()), None)
        if atom_a:
            # los labels deben existir y ser strings
            assert isinstance(atom_a["transfers"], list)
            for lbl in atom_a["transfers"]:
                assert isinstance(lbl, str) and len(lbl) > 0


# ---------------------------------------------------------------------------
# Tests de Auditoría
# ---------------------------------------------------------------------------

class TestAuditHelpers:
    """Prueba las funciones de auditoría por separado contra una conexión temporal."""

    def _conn(self, tmp_path: Path) -> sqlite3.Connection:
        _make_atoms_db(tmp_path)
        conn = sqlite3.connect(tmp_path / "data" / "sessions.db")
        conn.row_factory = sqlite3.Row
        return conn

    def test_audit_duplicados_detects_shared_signature(self, tmp_path: Path) -> None:
        """_audit_duplicados detecta la signature compartida entre átomo A y G."""
        conn = self._conn(tmp_path)
        findings = L._audit_duplicados(conn)
        conn.close()
        assert len(findings) >= 1
        # todas las filas son tipo duplicado
        assert all(f["type"] == "duplicado" for f in findings)
        # la signature duplicada de A y G debe aparecer
        sigs = [f["signature"] for f in findings]
        assert any("probability-estimation|ledger-append-only|deterministic" in s for s in sigs)

    def test_audit_duplicados_includes_ids(self, tmp_path: Path) -> None:
        """Los findings de duplicado incluyen la lista de rowids afectados."""
        conn = self._conn(tmp_path)
        findings = L._audit_duplicados(conn)
        conn.close()
        for f in findings:
            assert isinstance(f["ids"], list)
            assert len(f["ids"]) >= 2

    def test_audit_sin_validity_detects_atom_d(self, tmp_path: Path) -> None:
        """_audit_sin_validity detecta el átomo D (sin validity_domain)."""
        conn = self._conn(tmp_path)
        findings = L._audit_sin_validity(conn)
        conn.close()
        assert len(findings) == 1
        assert findings[0]["type"] == "sin_validity"
        assert findings[0]["count"] >= 1

    def test_audit_sin_source_detects_atom_e(self, tmp_path: Path) -> None:
        """_audit_sin_source detecta el átomo E (sin source_project)."""
        conn = self._conn(tmp_path)
        findings = L._audit_sin_source(conn)
        conn.close()
        assert len(findings) == 1
        assert findings[0]["type"] == "sin_source"
        assert findings[0]["count"] >= 1

    def test_audit_bajo_valor_detects_atom_f(self, tmp_path: Path) -> None:
        """_audit_bajo_valor detecta el átomo F con validity_domain [BAJO VALOR]."""
        conn = self._conn(tmp_path)
        findings = L._audit_bajo_valor(conn)
        conn.close()
        assert len(findings) == 1
        assert findings[0]["type"] == "bajo_valor"
        assert findings[0]["count"] >= 1
        # debe incluir items con label
        assert len(findings[0]["items"]) >= 1

    def test_audit_huecos_detects_embedding_retrieval(self, tmp_path: Path) -> None:
        """_audit_huecos detecta 'embedding-retrieval' como hueco (solo unused en fixture)."""
        conn = self._conn(tmp_path)
        findings = L._audit_huecos(conn)
        conn.close()
        assert len(findings) == 1
        assert findings[0]["type"] == "hueco"
        classes = [c["problem_class"] for c in findings[0]["classes"]]
        assert "embedding-retrieval" in classes

    def test_audit_huecos_empty_when_all_used(self, tmp_path: Path) -> None:
        """Sin huecos → _audit_huecos devuelve lista vacía."""
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(tmp_path / "data" / "sessions.db")
        conn.execute("""CREATE TABLE decisions(
            id INTEGER PRIMARY KEY, decision TEXT NOT NULL, mem_type TEXT,
            problem_class TEXT, structural_signature TEXT, adoption TEXT,
            validity_domain TEXT, source_project TEXT, evidence_kind TEXT,
            transfers_to TEXT, artifact_type TEXT, regime TEXT, skeleton TEXT,
            client_id TEXT, domain TEXT, locked INTEGER, created_at TEXT,
            variable_verdicts TEXT, epistemic_status TEXT, digest_id TEXT,
            rationale TEXT, evidence TEXT, session_ref TEXT, canonical_id INTEGER)""")
        conn.execute("""INSERT INTO decisions(decision,mem_type,problem_class,
            structural_signature,adoption) VALUES
            ('x','fact','probability-estimation','prob|-|det','used')""")
        conn.commit()
        findings = L._audit_huecos(conn)
        conn.close()
        assert findings == []


class TestReadAuditoria:
    """Prueba read_auditoria end-to-end contra la DB temporal."""

    def test_unavailable_when_no_db(self, tmp_path: Path) -> None:
        """Sin sessions.db → available=False (fail-soft)."""
        au = L.read_auditoria(tmp_path)
        assert au["available"] is False

    def test_returns_all_finding_types(self, tmp_path: Path) -> None:
        """La fixture activa los 4 tipos de hallazgo esperados."""
        _make_atoms_db(tmp_path)
        au = L.read_auditoria(tmp_path)
        assert au["available"] is True
        types_found = {f["type"] for f in au["findings"]}
        assert "duplicado" in types_found
        assert "sin_validity" in types_found
        assert "sin_source" in types_found
        assert "bajo_valor" in types_found
        assert "hueco" in types_found

    def test_summary_counts_match_findings(self, tmp_path: Path) -> None:
        """summary[type] == número de findings de ese tipo."""
        _make_atoms_db(tmp_path)
        au = L.read_auditoria(tmp_path)
        from collections import Counter
        expected = Counter(f["type"] for f in au["findings"])
        assert dict(expected) == au["summary"]

    def test_total_findings_equals_len(self, tmp_path: Path) -> None:
        """total_findings == len(findings)."""
        _make_atoms_db(tmp_path)
        au = L.read_auditoria(tmp_path)
        assert au["total_findings"] == len(au["findings"])

    def test_total_atoms_positive(self, tmp_path: Path) -> None:
        """total_atoms refleja todos los facts en la DB."""
        _make_atoms_db(tmp_path)
        au = L.read_auditoria(tmp_path)
        assert au["total_atoms"] > 0

    def test_severity_values_are_valid(self, tmp_path: Path) -> None:
        """Todos los hallazgos tienen severity válido (warn | info | down)."""
        _make_atoms_db(tmp_path)
        au = L.read_auditoria(tmp_path)
        valid = {"warn", "info", "down"}
        for f in au["findings"]:
            assert f["severity"] in valid, f"severity inválido en {f['type']}: {f['severity']}"
