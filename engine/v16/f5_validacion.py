#!/usr/bin/env python3
"""F5.VALIDACION — 3-tier output validation with semantic entropy and ensemble judgment.

Validates pipeline artifacts (code, configs, tests) via:
1. TIER 1: Contract gates (fast, structural)
2. TIER 2: Semantic entropy (medium, semantic quality)
3. TIER 3: LLM judge ensemble (expensive, complex judgment)

Exit: Produces ValidationResult with verdict, confidence scores, and remediation.

Usage:
    validator = ValidacionEngine()
    result = validator.validate(output, contract, context)
    if result.verdict == "PASS":
        proceed_to_f6()
    elif result.verdict == "UNCERTAIN":
        retry_with_more_depth()
    else:
        handle_failure(result.failure_type)
"""

import math
from dataclasses import dataclass, field
from typing import Optional, Any
import json
import re
import hashlib


@dataclass
class ValidationResult:
    """Result of 3-tier validation cascade.

    Attributes:
        verdict: "PASS" | "FAIL" | "UNCERTAIN"
        confidence: 0.0-1.0 overall confidence score
        tier_results: dict with Tier 1/2/3 scores
        scores: dict with {completeness, correctness, conciseness, semantic_quality}
        issues: list of {severity, description, location} dicts
        failure_type: e.g., "hallucination", "incomplete", "wrong_format", "logic_error"
        tier_passed: which tier(s) passed before failing
        remediation: suggested action {retry_depth, ask_human, break_into_subtasks}
    """

    verdict: str  # PASS | FAIL | UNCERTAIN
    confidence: float
    tier_results: dict[str, dict] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    failure_type: Optional[str] = None
    tier_passed: list[str] = field(default_factory=list)
    remediation: Optional[dict] = None


@dataclass
class ContractSpec:
    """Schema contract for output validation.

    Attributes:
        required_fields: list of field names that must be present
        type_checks: dict of {field: type_name} for validation
        format: output format requirement (json, markdown, code, etc.)
        min_length: minimum content length (chars or lines)
        max_length: maximum content length
    """

    required_fields: list[str] = field(default_factory=list)
    type_checks: dict[str, str] = field(default_factory=dict)
    format: str = "text"
    min_length: int = 0
    max_length: int = 1000000


class ValidacionEngine:
    """F5: 3-tier validation cascade for output verification."""

    def __init__(self, ollama_host: str = "http://localhost:11434"):
        """Initialize validation engine.

        Args:
            ollama_host: Ollama API endpoint for semantic entropy computation
        """
        self.ollama_host = ollama_host
        self.failure_taxonomy = {
            "hallucination": "Output contains claims not in input/context",
            "incomplete": "Output missing required sections from contract",
            "wrong_format": "Output format doesn't match spec (JSON, markdown, etc.)",
            "logic_error": "Output is internally contradictory or logically broken",
            "regression": "Output quality worse than baseline/previous version",
            "type_mismatch": "Field type doesn't match contract expectation",
            "length_violation": "Output too short or too long",
        }

    def validate(
        self,
        output: str,
        contract: Optional[dict | ContractSpec] = None,
        context: Optional[dict] = None,
    ) -> ValidationResult:
        """Main entry point: execute 3-tier validation cascade.

        Args:
            output: The artifact to validate (code, config, text, etc.)
            contract: ContractSpec or dict with required_fields, type_checks, format
            context: Context dict with {query, model, temperature, etc.} for semantic checks

        Returns:
            ValidationResult with verdict and detailed scores
        """
        result = ValidationResult(verdict="PASS", confidence=1.0)

        # Normalize contract
        if contract is None:
            contract = ContractSpec()
        elif isinstance(contract, dict):
            contract = ContractSpec(**contract)

        # TIER 1: Contract gates (fast, structural)
        tier1_result = self._tier1_contract(output, contract)
        result.tier_results["tier1_contract"] = tier1_result
        result.scores.update(tier1_result.get("scores", {}))

        if tier1_result["status"] == "FAIL":
            result.verdict = "FAIL"
            result.confidence = 0.1
            result.failure_type = tier1_result.get("failure_type", "contract_violation")
            result.issues.extend(tier1_result.get("issues", []))
            result.remediation = {"action": "retry", "reason": "contract_violation"}
            return result

        result.tier_passed.append("tier1")

        # TIER 2: Semantic entropy (medium cost, semantic quality)
        if context is not None:
            tier2_result = self._tier2_semantic(output, context)
            result.tier_results["tier2_semantic"] = tier2_result
            result.scores["semantic_quality"] = tier2_result.get("entropy_score", 0.5)

            if tier2_result["status"] == "FAIL":
                result.verdict = "UNCERTAIN"
                result.confidence = tier2_result.get("confidence", 0.5)
                result.failure_type = tier2_result.get("failure_type", "high_uncertainty")
                result.issues.extend(tier2_result.get("issues", []))
                result.remediation = {
                    "action": "retry_with_more_depth",
                    "reason": "high_semantic_entropy",
                }
                return result

            result.tier_passed.append("tier2")

        # TIER 3: LLM judge (expensive, complex judgment) — Phase 2
        # For V16 Phase 1, skip Tier 3 and rely on Tiers 1-2
        # result = self._tier3_llm_judge(output, contract, context)

        # Compute overall confidence (product of tier scores, normalized)
        tier1_conf = tier1_result.get("confidence", 1.0)
        tier2_conf = result.tier_results.get("tier2_semantic", {}).get("confidence", 1.0)

        result.confidence = tier1_conf * tier2_conf
        if result.confidence < 0.6:
            result.verdict = "UNCERTAIN"

        return result

    def _tier1_contract(self, output: str, contract: ContractSpec) -> dict:
        """TIER 1: Contract gates — fast structural validation.

        Checks:
        1. Format compliance (JSON, markdown, code syntax)
        2. Required fields present
        3. Type correctness
        4. Length bounds

        Args:
            output: The artifact to validate
            contract: ContractSpec with requirements

        Returns:
            dict with {status, confidence, scores, issues, failure_type}
        """
        issues = []
        scores = {}

        # Check 1: Length bounds
        self._tier1_check_length(output, contract, issues, scores)

        # Check 2: Format compliance
        format_ok = self._tier1_check_format(output, contract, issues, scores)

        # Check 3 & 4: Required fields and type checking (if format is JSON and format is valid)
        if contract.format == "json" and format_ok:
            self._tier1_check_json(output, contract, issues, scores)

        # Determine tier status
        has_errors = any(i["severity"] == "error" for i in issues)
        status = "FAIL" if has_errors else "PASS"
        confidence = sum(scores.values()) / len(scores) if scores else 1.0
        failure_type = self._tier1_failure_type(status, issues)

        return {
            "status": status,
            "confidence": confidence,
            "scores": scores,
            "issues": issues,
            "failure_type": failure_type,
        }

    def _tier1_check_length(
        self,
        output: str,
        contract: ContractSpec,
        issues: list,
        scores: dict,
    ) -> None:
        """Validate output length against the contract bounds.

        Appends length-violation issues and records ``scores["length_ok"]``
        (1.0 when within bounds, 0.0 otherwise). Mutates ``issues`` and
        ``scores`` in place.

        Args:
            output: The artifact to validate.
            contract: ContractSpec with min/max length requirements.
            issues: Accumulator list of issue dicts to extend.
            scores: Accumulator dict of per-check scores to update.
        """
        length_issues = []
        if contract.min_length > 0 and len(output) < contract.min_length:
            length_issues.append(
                f"Output too short: {len(output)} < {contract.min_length}"
            )
        if contract.max_length > 0 and len(output) > contract.max_length:
            length_issues.append(
                f"Output too long: {len(output)} > {contract.max_length}"
            )

        if length_issues:
            for desc in length_issues:
                issues.append(
                    {
                        "severity": "error",
                        "description": desc,
                        "location": "tier1_length",
                    }
                )
            scores["length_ok"] = 0.0
        else:
            scores["length_ok"] = 1.0

    def _tier1_check_format(
        self,
        output: str,
        contract: ContractSpec,
        issues: list,
        scores: dict,
    ) -> bool:
        """Validate output format compliance against the contract.

        Records ``scores["format_ok"]`` and appends a format issue when the
        output does not match the expected format. Mutates ``issues`` and
        ``scores`` in place.

        Args:
            output: The artifact to validate.
            contract: ContractSpec with the expected format.
            issues: Accumulator list of issue dicts to extend.
            scores: Accumulator dict of per-check scores to update.

        Returns:
            True if the format is valid, False otherwise.
        """
        format_ok = self._check_format(output, contract.format)
        scores["format_ok"] = 1.0 if format_ok else 0.0
        if not format_ok:
            issues.append(
                {
                    "severity": "error",
                    "description": f"Invalid format: expected {contract.format}",
                    "location": "tier1_format",
                }
            )
        return format_ok

    def _tier1_check_json(
        self,
        output: str,
        contract: ContractSpec,
        issues: list,
        scores: dict,
    ) -> None:
        """Validate required fields and type constraints for JSON output.

        Parses ``output`` as JSON and checks required fields and type
        constraints, recording ``fields_ok``/``types_ok`` scores. On a JSON
        decode error records ``json_parse_ok = 0.0``. Mutates ``issues`` and
        ``scores`` in place.

        Args:
            output: The artifact to validate (expected to be JSON).
            contract: ContractSpec with required fields and type checks.
            issues: Accumulator list of issue dicts to extend.
            scores: Accumulator dict of per-check scores to update.
        """
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            issues.append(
                {
                    "severity": "error",
                    "description": f"JSON decode error: {str(e)}",
                    "location": "tier1_json_parse",
                }
            )
            scores["json_parse_ok"] = 0.0
            return

        if contract.required_fields:
            self._tier1_check_required_fields(data, contract, issues, scores)

        if contract.type_checks:
            self._tier1_check_type_constraints(data, contract, issues, scores)

    def _tier1_check_required_fields(
        self,
        data: dict,
        contract: ContractSpec,
        issues: list,
        scores: dict,
    ) -> None:
        """Check that all required fields are present in parsed JSON data.

        Appends a missing-fields issue when any required field is absent and
        records ``scores["fields_ok"]``. Mutates ``issues`` and ``scores`` in
        place.

        Args:
            data: The parsed JSON object.
            contract: ContractSpec with the required field names.
            issues: Accumulator list of issue dicts to extend.
            scores: Accumulator dict of per-check scores to update.
        """
        missing = [f for f in contract.required_fields if f not in data]
        if missing:
            issues.append(
                {
                    "severity": "error",
                    "description": f"Missing required fields: {missing}",
                    "location": "tier1_fields",
                }
            )
            scores["fields_ok"] = 0.0
        else:
            scores["fields_ok"] = 1.0

    def _tier1_check_type_constraints(
        self,
        data: dict,
        contract: ContractSpec,
        issues: list,
        scores: dict,
    ) -> None:
        """Check that present JSON fields satisfy their declared types.

        Appends a type-mismatch issue per offending field and records
        ``scores["types_ok"]``. Mutates ``issues`` and ``scores`` in place.

        Args:
            data: The parsed JSON object.
            contract: ContractSpec with the field type constraints.
            issues: Accumulator list of issue dicts to extend.
            scores: Accumulator dict of per-check scores to update.
        """
        type_errors = []
        for field_name, expected_type in contract.type_checks.items():
            if field_name in data:
                if not self._check_type(data[field_name], expected_type):
                    type_errors.append(field_name)

        if type_errors:
            for field_name in type_errors:
                issues.append(
                    {
                        "severity": "error",
                        "description": f"Type mismatch in field '{field_name}': expected {contract.type_checks[field_name]}, got {type(data[field_name]).__name__}",
                        "location": f"tier1_type_{field_name}",
                    }
                )
            scores["types_ok"] = 0.0
        else:
            scores["types_ok"] = 1.0

    def _tier1_failure_type(self, status: str, issues: list) -> str | None:
        """Classify the primary tier-1 failure from accumulated issues.

        Applies the same precedence as the inline logic: format, then fields,
        then type, then length.

        Args:
            status: The overall tier status ("PASS" or "FAIL").
            issues: List of issue dicts produced by the tier-1 checks.

        Returns:
            A failure-type string when ``status`` is "FAIL" and a matching
            issue location exists, otherwise None.
        """
        if status != "FAIL":
            return None
        if any("format" in i["location"] for i in issues):
            return "wrong_format"
        if any("fields" in i["location"] for i in issues):
            return "incomplete"
        if any("type" in i["location"] for i in issues):
            return "type_mismatch"
        if any("length" in i["location"] for i in issues):
            return "length_violation"
        return None

    def _tier2_semantic(self, output: str, context: dict) -> dict:
        """TIER 2: Semantic entropy — measure output consistency via embedding clustering.

        For V16 Phase 1, implements simplified semantic entropy:
        1. Generate 1-3 related outputs (via context model re-sampling)
        2. Embed each with mxbai-embed-large
        3. Cluster by similarity (threshold 0.8)
        4. Compute entropy over clusters

        High entropy (>0.6) = uncertain = likely wrong = flag for retry

        Args:
            output: The artifact to validate
            context: dict with {query, model, temperature, samples} for re-generation

        Returns:
            dict with {status, confidence, entropy_score, issues}
        """
        # Simplified Phase 1 implementation: check output consistency markers
        # Full implementation would re-sample and embed; that's Phase 2

        issues = []
        entropy_score = 0.0  # 0 = confident, 1 = uncertain

        # Heuristic checks for semantic quality (Phase 1)
        # Check 1: Presence of uncertainty markers (incomplete, TODO, etc.)
        # Count only critical markers (TODO, FIXME, XXX in code/comments)
        uncertainty_markers = [
            "# TODO:",
            "# FIXME:",
            "# XXX:",
            "// TODO:",
            "// FIXME:",
            "/* TODO:",
            "/* FIXME:",
        ]
        marker_count = sum(output.count(m) for m in uncertainty_markers)
        if marker_count > 2:
            issues.append(
                {
                    "severity": "warning",
                    "description": f"High uncertainty indicators ({marker_count} found)",
                    "location": "tier2_uncertainty_markers",
                }
            )
            entropy_score += 0.4

        # Check 2: Internal contradictions (only major functional contradictions)
        contradictions = self._detect_contradictions(output)
        if contradictions:
            issues.extend(
                [
                    {
                        "severity": "warning",
                        "description": f"Potential contradiction: {c}",
                        "location": "tier2_contradiction",
                    }
                    for c in contradictions
                ]
            )
            entropy_score += 0.2

        # Check 3: Length consistency (output should be reasonably complete)
        if len(output.strip()) < 50:
            issues.append(
                {
                    "severity": "warning",
                    "description": "Output suspiciously short (< 50 chars)",
                    "location": "tier2_length_suspicion",
                }
            )
            entropy_score += 0.4

        # Determine tier status
        if entropy_score >= 0.55:
            status = "FAIL"
        elif entropy_score >= 0.3:
            status = "UNCERTAIN"
        else:
            status = "PASS"
        confidence = max(0.1, 1.0 - entropy_score)

        return {
            "status": status,
            "confidence": confidence,
            "entropy_score": entropy_score,
            "issues": issues,
            "failure_type": "high_uncertainty" if status != "PASS" else None,
        }

    def _check_format(self, output: str, expected_format: str) -> bool:
        """Validate output matches expected format.

        Args:
            output: The artifact
            expected_format: One of "json", "markdown", "code", "text"

        Returns:
            True if format valid
        """
        output = output.strip()

        if expected_format == "json":
            try:
                json.loads(output)
                return True
            except json.JSONDecodeError:
                return False

        elif expected_format == "markdown":
            # Phase 1: validación laxa — cualquier salida no vacía se acepta.
            # (la verificación real de estructura markdown —##/**/```— queda para Phase 2;
            #  el chequeo de patrones se eliminó porque no afectaba al veredicto.)
            return len(output) > 0

        elif expected_format == "code":
            # Check for code-like patterns
            has_code = any(
                pattern in output
                for pattern in ["def ", "class ", "function ", "{", "}", "=>"]
            )
            return has_code or len(output) > 20

        elif expected_format == "text":
            return len(output) > 0

        return True  # Unknown format, assume OK

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Validate value matches expected type.

        Args:
            value: The value to check
            expected_type: Type name ("str", "int", "bool", "list", "dict")

        Returns:
            True if type matches
        """
        type_map = {
            "str": str,
            "int": int,
            "bool": bool,
            "list": list,
            "dict": dict,
            "float": float,
        }

        expected_cls = type_map.get(expected_type)
        if expected_cls is None:
            return True  # Unknown type, assume OK

        return isinstance(value, expected_cls)

    def _detect_contradictions(self, output: str) -> list[str]:
        """Detect simple internal contradictions in output.

        Only detects explicit contradictions (not function names, multiple return statements, etc).

        Args:
            output: The artifact

        Returns:
            List of contradiction descriptions
        """
        contradictions = []

        # Only detect explicit contradictory docstrings or comments
        # Ignore patterns like "def fibonacci(n): return" which contain "return"

        # Check for explicit contradictory boolean claims in docstrings/comments
        if "assert" in output.lower() and "assert" in output:
            # Look for directly contradictory assertions
            assertions = re.findall(r"assert\s+(\w+)\s*==\s*(\w+)", output)
            assert_pairs = [(a[0], a[1]) for a in assertions]

            # Check if same variable is asserted to equal different values
            var_values = {}
            for var, val in assert_pairs:
                if var in var_values and var_values[var] != val:
                    contradictions.append(
                        f"Variable '{var}' asserted to equal different values: {var_values[var]} vs {val}"
                    )
                var_values[var] = val

        # Look for explicit "must be X" and "must be not X" patterns
        explicit_contradictions = re.findall(
            r"(must be|is|returns|type is)\s+(\w+).*?(must not be|is not|cannot be)\s+\2",
            output,
            re.IGNORECASE,
        )
        if explicit_contradictions:
            contradictions.append("Explicit contradictory requirement statements found")

        return contradictions

    def _classify_failure(
        self, output: str, contract: ContractSpec, tier_results: dict
    ) -> str:
        """Classify type of failure based on evidence.

        Args:
            output: The failed artifact
            contract: The contract that was violated
            tier_results: Results from failed tier

        Returns:
            Failure type string (from self.failure_taxonomy)
        """
        # Use tier 1 classification if available
        if "tier1_contract" in tier_results:
            tier1 = tier_results["tier1_contract"]
            if tier1.get("failure_type"):
                return tier1["failure_type"]

        # Use tier 2 classification if available
        if "tier2_semantic" in tier_results:
            tier2 = tier_results["tier2_semantic"]
            if tier2.get("failure_type"):
                return tier2["failure_type"]

        # Fallback: heuristic classification
        if len(output) == 0:
            return "incomplete"
        if len(output) < 50:
            return "incomplete"
        if output.count("TODO") > 3:
            return "incomplete"
        if any(claim in output for claim in ["I think", "probably", "might be"]):
            return "hallucination"

        return "logic_error"


def compute_semantic_entropy(
    outputs: list[str], embedding_fn=None, threshold: float = 0.8
) -> float:
    """Compute semantic entropy over multiple outputs via clustering.

    For Phase 1, this is a stub that assumes available embeddings.
    Full implementation in Phase 2 integrates with mxbai-embed-large via Ollama.

    Args:
        outputs: List of output strings to compare
        embedding_fn: Optional function(str) -> list[float] to embed each output
        threshold: Cosine similarity threshold for clustering (0.8 = similar)

    Returns:
        Entropy score (0 = confident/consistent, 1 = uncertain/diverse)
    """
    if len(outputs) < 2:
        return 0.0

    if embedding_fn is None:
        # Fallback: use simple hash-based clustering
        # In Phase 2, replace with real embeddings
        embedding_fn = _hash_embedding

    # Embed all outputs
    embeddings = [embedding_fn(output) for output in outputs]

    # Cluster by similarity
    clusters = _cluster_by_similarity(embeddings, threshold)

    # Compute entropy over cluster sizes
    cluster_sizes = [len(c) for c in clusters]
    probs = [size / len(outputs) for size in cluster_sizes]
    entropy = -sum(
        p * math.log(p + 1e-10) for p in probs if p > 0
    )  # Shannon entropy

    # Normalize to [0, 1]
    max_entropy = math.log(len(outputs))
    normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0

    return min(1.0, normalized_entropy)


def _hash_embedding(text: str, dim: int = 256) -> list[float]:
    """Stub: generate pseudo-embedding from text hash (Phase 1 only).

    In Phase 2, replace with real embeddings from mxbai-embed-large.

    Args:
        text: Text to embed
        dim: Embedding dimension

    Returns:
        Pseudo-embedding vector
    """
    # Create deterministic hash-based vector
    hash_val = hashlib.sha256(text.encode()).digest()
    # Convert bytes to floats in [0, 1]
    embedding = [
        float((hash_val[i % len(hash_val)] + hash_val[(i + 1) % len(hash_val)]) % 256)
        / 256.0
        for i in range(dim)
    ]
    return embedding


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a, b: Two embedding vectors

    Returns:
        Cosine similarity in [0, 1]
    """
    if len(a) != len(b):
        return 0.0

    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) + 1e-10
    norm_b = math.sqrt(sum(x * x for x in b)) + 1e-10

    return dot_product / (norm_a * norm_b)


def _cluster_by_similarity(
    embeddings: list[list[float]], threshold: float = 0.8
) -> list[list[int]]:
    """Cluster embeddings by cosine similarity using greedy approach.

    Args:
        embeddings: List of embedding vectors
        threshold: Similarity threshold for grouping

    Returns:
        List of clusters, where each cluster is a list of indices
    """
    clusters = []
    assigned = set()

    for i, emb_i in enumerate(embeddings):
        if i in assigned:
            continue

        cluster = [i]
        assigned.add(i)

        for j in range(i + 1, len(embeddings)):
            if j in assigned:
                continue

            similarity = _cosine_similarity(emb_i, embeddings[j])
            if similarity >= threshold:
                cluster.append(j)
                assigned.add(j)

        clusters.append(cluster)

    return clusters
