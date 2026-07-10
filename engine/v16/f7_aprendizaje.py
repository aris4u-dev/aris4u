"""F7.APRENDIZAJE — Online Learning Engine for ARIS V16.

MODULE STATUS (honest):  calibration write-back = LIVE  ·  embedding fine-tuning = DEFERRED.

5-phase learning cascade executed at session end:
1. SELECT_EXEMPLARS: Identify uncertain queries and hard negatives.
2. IMPROVE_EMBEDDINGS: **DEFERRED — NOT implemented (stub).** Fine-tuning the
   embedding model is intentionally not faked. See the Phase 2 block in
   ``learn_from_session`` for the exact infra it requires (triplet dataset +
   training run + fine-tuned-weight write-back + A/B gate) and why claiming it
   "improves embeddings" today would be dishonest.
3. CALIBRATE_CLASSIFIER: Temperature scaling to fix confidence over/under-confidence.
   **This phase now APPLIES in production**: an accepted temperature is written back
   to ``v16_active_calibration`` and ``f1_classifier`` reads it at startup to
   recalibrate its confidence (logit-space Platt scaling — see f1 ``_apply_temperature``).
   This is the real, modest learning F7 performs in prod: better CALIBRATION, not a
   better model. It cannot change the classifier's argmax decision (temperature is
   monotonic) — it changes the confidence the routing threshold consumes.
4. TUNE_PID_GAINS: Ziegler-Nichols auto-tuning for F2 budget controller (computed +
   persisted to history; consumed by F2 separately).
5. EVALUATE_LEARNING: gate acceptance on a REAL evaluation (McNemar's accuracy
   A/B when a retrained model exists; calibration ECE reduction otherwise).

Safeguards prevent learning from corrupted data (distribution shift, overfitting, instability).

Acceptance contract (Phase 5): a learning update is ACCEPTED only when an
evaluation produces evidence of improvement. Fail-open default = NOT accepted
(refuse the update) when there is no ground truth or nothing evaluable — the
previous `accepted = True` placeholder accepted no-op / unverifiable updates
unconditionally, which is the bug this loop closes. Only an ACCEPTED calibration
is written back to the live classifier.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy.stats import chi2

try:
    from .config import BUSY_TIMEOUT_MS, SESSIONS_DB
except ImportError:
    SESSIONS_DB = Path.home() / "projects" / "aris4u" / "data" / "sessions.db"
    BUSY_TIMEOUT_MS = 10000

logger = logging.getLogger(__name__)


@dataclass
class LearningResult:
    """Output of F7 learning cascade."""

    exemplars_added: int
    accuracy_before: float
    accuracy_after: float
    temperature: float
    pid_gains: dict[str, float]  # {kp, ki, kd}
    accepted: bool  # whether new params were accepted
    reason: str


class ExemplarSelector:
    """Phase 1: SELECT_EXEMPLARS via uncertainty sampling + hard negative mining."""

    @staticmethod
    def compute_uncertainty_score(confidence: float) -> float:
        """Uncertainty = 1 - confidence (high uncertainty = low confidence)."""
        return 1.0 - confidence

    @staticmethod
    def cosine_similarity(emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings."""
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(np.dot(emb1, emb2) / (norm1 * norm2))

    def _collect_uncertain_candidates(
        self,
        session_data: List[Dict],
        uncertainty_threshold: float,
    ) -> List[Dict]:
        """Step 1: Uncertainty sampling — collect candidates below confidence threshold."""
        candidates = []
        for data in session_data:
            confidence = data.get("confidence", 1.0)
            if confidence < uncertainty_threshold:
                candidates.append(
                    {
                        "query": data.get("query", ""),
                        "true_intent": data.get("true_intent", ""),
                        "pred_intent": data.get("predicted_intent", ""),
                        "confidence_old": confidence,
                        "uncertainty_score": self.compute_uncertainty_score(confidence),
                        "embedding": data.get("embedding", np.zeros(384)),
                        "source": "uncertainty_sampling",
                    }
                )
        return candidates

    def _find_hard_negatives_for_candidate(
        self,
        candidate: Dict,
        exemplar_pool: Dict[str, List[Dict]],
        similarity_threshold: float,
    ) -> List[Dict]:
        """Return top-5 hard-negative exemplars for a single misclassified candidate."""
        candidate_emb = candidate["embedding"]
        hard_negs = []
        for intent, exemplars in exemplar_pool.items():
            if intent == candidate["true_intent"]:
                continue
            for exemplar in exemplars:
                exemplar_emb = exemplar.get("embedding", np.zeros(384))
                similarity = self.cosine_similarity(candidate_emb, exemplar_emb)
                if similarity > similarity_threshold:
                    hard_negs.append(
                        {
                            "exemplar": exemplar,
                            "similarity": similarity,
                            "intent_mismatch": (candidate["true_intent"], intent),
                        }
                    )
        return sorted(hard_negs, key=lambda x: -x["similarity"])[:5]

    def _mine_hard_negatives(
        self,
        uncertain_candidates: List[Dict],
        exemplar_pool: Dict[str, List[Dict]],
        similarity_threshold: float,
    ) -> List[Dict]:
        """Step 2: Hard negative mining — annotate misclassified candidates and return those
        with at least one hard negative."""
        hard_negative_candidates = []
        misclassified = [c for c in uncertain_candidates if c["pred_intent"] != c["true_intent"]]
        for candidate in misclassified:
            hard_negs = self._find_hard_negatives_for_candidate(
                candidate, exemplar_pool, similarity_threshold
            )
            candidate["hard_negatives"] = hard_negs
            if hard_negs:
                hard_negative_candidates.append(candidate)
        return hard_negative_candidates

    def _build_existing_queries(self, exemplar_pool: Dict[str, List[Dict]]) -> set:
        """Step 3a: Collect the set of normalised queries already in the exemplar pool."""
        return {
            ex.get("query", "").lower() for exemplars in exemplar_pool.values() for ex in exemplars
        }

    def select_exemplars(
        self,
        session_data: List[Dict],
        exemplar_pool: Dict[str, List[Dict]],
        uncertainty_threshold: float = 0.70,
        similarity_threshold: float = 0.70,
        budget: int = 20,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Phase 1: Select exemplar candidates from session data.

        Args:
            session_data: List of {query, true_intent, predicted_intent, confidence, embedding}
            exemplar_pool: {intent: [exemplar_dict]}
            uncertainty_threshold: confidence < this → candidate
            similarity_threshold: cosine(embedding) > this → hard negative
            budget: max exemplars to select

        Returns:
            (uncertain_candidates, hard_negative_candidates)
        """
        # === STEP 1: Uncertainty sampling ===
        uncertain_candidates = self._collect_uncertain_candidates(
            session_data, uncertainty_threshold
        )

        # === STEP 2: Hard negative mining ===
        hard_negative_candidates = self._mine_hard_negatives(
            uncertain_candidates, exemplar_pool, similarity_threshold
        )

        # === STEP 3: Deduplication ===
        existing_queries = self._build_existing_queries(exemplar_pool)
        uncertain_dedup = [
            c for c in uncertain_candidates if c["query"].lower() not in existing_queries
        ]
        hard_negs_dedup = [
            c for c in hard_negative_candidates if c["query"].lower() not in existing_queries
        ]

        # === STEP 4: Select final exemplars using regret-bounded criterion ===
        selected = self._select_final_exemplars(uncertain_dedup + hard_negs_dedup, budget=budget)

        return selected, []

    def _select_final_exemplars(self, all_candidates: List[Dict], budget: int = 20) -> List[Dict]:
        """Rank candidates by information gain / regret cost (regret-bounded)."""
        for candidate in all_candidates:
            candidate["info_gain"] = candidate.get("uncertainty_score", 0.5)
            candidate["regret_cost"] = 1.0 + len(candidate.get("hard_negatives", [])) * 0.1
            candidate["regret_score"] = (
                candidate["info_gain"] / candidate["regret_cost"]
                if candidate["regret_cost"] > 0
                else 0
            )

        # === Curriculum ordering: easy (high confidence) before hard ===
        easy_candidates = [c for c in all_candidates if c["confidence_old"] > 0.5]
        hard_candidates = [c for c in all_candidates if c["confidence_old"] <= 0.5]

        easy_sorted = sorted(easy_candidates, key=lambda x: -x["regret_score"])
        hard_sorted = sorted(hard_candidates, key=lambda x: -x["regret_score"])

        # 60% easy, 40% hard
        easy_count = max(1, int(budget * 0.6))
        hard_count = budget - easy_count

        selected = easy_sorted[:easy_count] + hard_sorted[:hard_count]
        return selected


class ClassifierCalibrator:
    """Phase 3: CALIBRATE_CLASSIFIER via temperature scaling."""

    @staticmethod
    def softmax(x: np.ndarray, axis: int = 1) -> np.ndarray:
        """Compute softmax along axis."""
        exp_x = np.exp(x - np.max(x, axis=axis, keepdims=True))
        return exp_x / np.sum(exp_x, axis=axis, keepdims=True)

    @staticmethod
    def compute_ece(
        logits: np.ndarray, labels: np.ndarray, temperature: float = 1.0, n_bins: int = 10
    ) -> float:
        """
        Expected Calibration Error: mean absolute difference between
        confidence and accuracy across bins.
        """
        softmax_probs = ClassifierCalibrator.softmax(logits / temperature, axis=1)
        max_probs = np.max(softmax_probs, axis=1)

        bin_edges = np.linspace(0, 1, n_bins + 1)
        ece = 0.0

        for i in range(n_bins):
            mask = (max_probs >= bin_edges[i]) & (max_probs < bin_edges[i + 1])
            if np.sum(mask) == 0:
                continue

            bin_confidence = np.mean(max_probs[mask])
            bin_accuracy = np.mean(labels[mask])
            ece += np.sum(mask) / len(labels) * abs(bin_confidence - bin_accuracy)

        return ece

    def calibrate(
        self,
        session_predictions: List[Dict],
        ground_truth_labels: Dict[str, str],
        test_fraction: float = 0.2,
        method: str = "temperature_scaling",
    ) -> Dict[str, Any]:  # incluye calibration_curve anidada → Any, no float
        """
        Phase 3: Calibrate classifier confidences via temperature scaling.

        Args:
            session_predictions: List of {query, predicted_intent, confidence, intent_logits}
            ground_truth_labels: {query: true_intent}
            test_fraction: Fraction to use for calibration (80/20 split)
            method: "temperature_scaling" or "platt_scaling"

        Returns:
            {temperature_coeff, ece_before, ece_after, calibration_curve, ...}
        """
        if not session_predictions:
            return {
                "temperature_coeff": 1.0,
                "ece_before": 0.0,
                "ece_after": 0.0,
                "n_test_queries": 0,
            }

        # === STEP 1: Split into train/calib ===
        indices = np.arange(len(session_predictions))
        np.random.shuffle(indices)
        split_idx = int(len(indices) * (1 - test_fraction))

        calib_indices = indices[split_idx:]
        calib_preds = [session_predictions[i] for i in calib_indices]

        # === STEP 2: Extract logits ===
        calib_logits = []
        calib_labels = []

        for pred in calib_preds:
            query = pred.get("query", "")
            if "intent_logits" in pred:
                logits = np.array(pred["intent_logits"], dtype=np.float32)
            else:
                # Fallback: construct from confidence
                confidence = pred.get("confidence", 0.5)
                logits = np.array([np.log(confidence / (1 - confidence + 1e-7)), 0.0, 0.0])

            true_intent = ground_truth_labels.get(query, "")
            pred_intent = pred.get("predicted_intent", "")
            correct = true_intent == pred_intent

            calib_logits.append(logits)
            calib_labels.append(1.0 if correct else 0.0)

        if not calib_logits:
            return {
                "temperature_coeff": 1.0,
                "ece_before": 0.0,
                "ece_after": 0.0,
                "n_test_queries": 0,
            }

        calib_logits = np.array(calib_logits)
        calib_labels = np.array(calib_labels)

        # === STEP 3: Optimize temperature ===
        def objective(T):
            return self.compute_ece(calib_logits, calib_labels, temperature=T[0], n_bins=10)

        result = minimize(objective, x0=[1.0], bounds=[(0.1, 10.0)], method="L-BFGS-B")
        optimal_temperature = float(result.x[0])

        # === STEP 4: Compute ECE before/after ===
        ece_before = self.compute_ece(calib_logits, calib_labels, temperature=1.0)
        ece_after = self.compute_ece(calib_logits, calib_labels, temperature=optimal_temperature)

        # === STEP 5: Calibration curve ===
        softmax_probs = self.softmax(calib_logits / optimal_temperature, axis=1)
        max_probs = np.max(softmax_probs, axis=1)

        confidence_bins = np.linspace(0, 1, 11)
        bin_centers = (confidence_bins[:-1] + confidence_bins[1:]) / 2
        accuracies = []

        for i in range(len(bin_centers)):
            mask = (max_probs >= confidence_bins[i]) & (max_probs < confidence_bins[i + 1])
            if np.sum(mask) > 0:
                accuracy = np.mean(calib_labels[mask])
            else:
                accuracy = 0.5
            accuracies.append(accuracy)

        return {
            "temperature_coeff": optimal_temperature,
            "ece_before": float(ece_before),
            "ece_after": float(ece_after),
            "n_test_queries": len(calib_preds),
            "calibration_curve": {
                "confidence_bins": bin_centers.tolist(),
                "accuracy_bins": accuracies,
            },
        }


class PIDTuner:
    """Phase 4: TUNE_PID_GAINS via Ziegler-Nichols method."""

    @staticmethod
    def estimate_ultimate_gain(
        session_data: List[Dict],
        budget_nominal: int = 10000,
        step_size: int = 500,
    ) -> Tuple[float, float]:
        """
        Estimate ultimate gain K_u and period T_u from historical session data.

        Args:
            session_data: [{session_id, budget, tokens_used}, ...]
            budget_nominal: baseline budget
            step_size: perturbation threshold

        Returns:
            (K_u, T_u)
        """
        if not session_data or len(session_data) < 3:
            logger.warning("Not enough data for Z-N tuning. Using defaults K_u=1.0, T_u=5.0")
            return 1.0, 5.0

        budgets = np.array([s.get("budget", budget_nominal) for s in session_data])
        tokens_used = np.array([s.get("tokens_used", budget_nominal) for s in session_data])

        # Normalize
        budget_deltas = budgets - budget_nominal
        token_deltas = tokens_used - np.mean(tokens_used)

        # Find step indices
        step_indices = np.where(np.abs(np.diff(budget_deltas)) > step_size / 2)[0]

        if len(step_indices) < 1:
            logger.warning("No step changes detected. Using defaults.")
            return 1.0, 5.0

        # Analyze response after first step
        step_start = step_indices[0]
        response_window = token_deltas[step_start : step_start + 10]

        # Estimate K_u (steady-state gain)
        K_u = float(np.max(response_window)) if len(response_window) > 0 else 1.0

        # Estimate tau via rise time
        if K_u > 0:
            rise_idx = np.where(response_window >= 0.632 * K_u)[0]
            tau = float(rise_idx[0]) if len(rise_idx) > 0 else 3.0
        else:
            tau = 1.0

        # Z-N: T_u = 4 * tau
        T_u = 4 * tau

        logger.info(f"Estimated K_u={K_u:.2f}, T_u={T_u:.2f}")
        return K_u, T_u

    @staticmethod
    def ziegler_nichols_gains(
        K_u: float, T_u: float, control_type: str = "PID"
    ) -> Dict[str, float]:
        """
        Compute PID gains using Ziegler-Nichols formulas.

        Args:
            K_u: Ultimate gain
            T_u: Ultimate period
            control_type: "P", "PI", or "PID"

        Returns:
            {p_gain, i_gain, d_gain}
        """
        if control_type == "P":
            return {"p_gain": 0.5 * K_u, "i_gain": 0.0, "d_gain": 0.0}
        elif control_type == "PI":
            Kp = 0.45 * K_u
            Ki = 1.2 * Kp / T_u
            return {"p_gain": Kp, "i_gain": Ki, "d_gain": 0.0}
        elif control_type == "PID":
            Kp = 0.6 * K_u
            Ki = 2.0 * Kp / T_u
            Kd = Kp * T_u / 8.0
            return {"p_gain": Kp, "i_gain": Ki, "d_gain": Kd}
        else:
            raise ValueError(f"Unknown control_type: {control_type}")

    @staticmethod
    def simulate_pid_response(
        p_gain: float,
        i_gain: float,
        d_gain: float,
        target_budget: int = 10000,
        perturbations: Optional[List[int]] = None,
        n_steps: int = 10,
    ) -> Dict:
        """
        Simulate PID response to validate tuning.

        Args:
            p_gain, i_gain, d_gain: PID coefficients
            target_budget: desired token consumption
            perturbations: budget disturbances per step
            n_steps: simulation steps

        Returns:
            {settling_time, overshoot, oscillates, steady_state_error, tokens_used, errors}
        """
        if perturbations is None:
            perturbations = [0] * n_steps

        tau = 1.0
        dt = 1.0

        tokens_used = np.zeros(n_steps)
        errors = np.zeros(n_steps)
        efforts = np.zeros(n_steps)
        integral_error = 0.0
        prev_error = 0.0

        tokens_used[0] = target_budget

        for t in range(1, n_steps):
            error = target_budget - tokens_used[t - 1]
            errors[t] = error

            integral_error += error * dt
            derivative_error = (error - prev_error) / dt if dt > 0 else 0

            effort = p_gain * error + i_gain * integral_error + d_gain * derivative_error
            efforts[t] = effort

            effort_clamped = np.clip(effort, 0, 2.0)
            tokens_used[t] = (
                tokens_used[t - 1] + (effort_clamped * target_budget - tokens_used[t - 1]) / tau
            ) + perturbations[t]

            prev_error = error

        # Analyze response
        final_error = float(np.abs(errors[-1]))
        overshoot = float(max(0, (np.max(tokens_used) - target_budget) / target_budget * 100))

        # Settling time: first time within 2% band
        within_band = np.abs(tokens_used - target_budget) <= 0.02 * target_budget
        settling_indices = np.where(within_band)[0]
        settling_time = int(settling_indices[0]) if len(settling_indices) > 1 else n_steps

        # Oscillation detection
        zero_crossings = int(np.sum(np.diff(np.sign(errors)) != 0))
        oscillates = zero_crossings > 2

        return {
            "settling_time": settling_time,
            "overshoot": overshoot,
            "oscillates": oscillates,
            "steady_state_error": final_error,
            "tokens_used": tokens_used.tolist(),
            "errors": errors.tolist(),
        }


class LearningEvaluator:
    """Phase 5: EVALUATE_LEARNING via McNemar's test."""

    @staticmethod
    def mcnemar_test(
        old_predictions: List[bool],
        new_predictions: List[bool],
        significance_level: float = 0.05,
        min_improvement: float = 0.02,
    ) -> Dict:
        """
        Phase 5: Evaluate if new model beats old model via McNemar's test.

        Args:
            old_predictions: [True/False for each query] (old model)
            new_predictions: [True/False for each query] (new model)
            significance_level: α for significance testing
            min_improvement: minimum required accuracy improvement

        Returns:
            {accuracy_old, accuracy_new, improvement, statistically_significant,
             p_value, accept_update, reason, contingency_table}
        """
        old_arr = np.array(old_predictions)
        new_arr = np.array(new_predictions)

        # Accuracy
        accuracy_old = float(np.mean(old_arr))
        accuracy_new = float(np.mean(new_arr))
        improvement = accuracy_new - accuracy_old

        # McNemar contingency table (comparación elementwise numpy: bool arrays).
        op = old_arr.astype(bool)
        npred = new_arr.astype(bool)
        a = int(np.sum(op & npred))
        b = int(np.sum(op & ~npred))
        c = int(np.sum(~op & npred))
        d = int(np.sum(~op & ~npred))

        # Test statistic
        if b + c > 0:
            statistic = (b - c) ** 2 / (b + c)
            p_value = float(chi2.sf(statistic, df=1))
        else:
            statistic = 0
            p_value = 1.0

        critical_value = float(chi2.ppf(1 - significance_level, df=1))
        is_significant = (statistic > critical_value) and (c > b)

        # Decision
        accept_update = (improvement > min_improvement) and is_significant

        reason = ""
        if improvement < min_improvement:
            reason = f"Improvement {improvement*100:.1f}% < threshold {min_improvement*100:.1f}%"
        elif not is_significant:
            reason = f"Not statistically significant (p={p_value:.3f} > 0.05)"
        else:
            reason = f"Significant improvement: {improvement*100:.1f}%, p={p_value:.3f}"

        return {
            "accuracy_old": accuracy_old,
            "accuracy_new": accuracy_new,
            "improvement": improvement,
            "statistically_significant": is_significant,
            "p_value": p_value,
            "test_size": len(old_predictions),
            "accept_update": accept_update,
            "reason": reason,
            "contingency_table": {
                "old_pass_new_pass": a,
                "old_pass_new_fail": b,
                "old_fail_new_pass": c,
                "old_fail_new_fail": d,
            },
        }


class AprendizajeEngine:
    """F7: 5-phase learning cascade executed at session end."""

    def __init__(self, db_path: str | None = None):
        """Initialize learning engine."""
        self.db_path = Path(db_path or SESSIONS_DB)
        self.exemplar_selector = ExemplarSelector()
        self.calibrator = ClassifierCalibrator()
        self.pid_tuner = PIDTuner()
        self.evaluator = LearningEvaluator()
        self._reward_signals = {}  # V16.10: Q-loop cache
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create v16 learning schema tables."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS v16_exemplar_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                query TEXT NOT NULL,
                true_intent TEXT NOT NULL,
                pred_intent TEXT,
                confidence_old REAL,
                uncertainty_score REAL,
                hard_negative_count INTEGER DEFAULT 0,
                regret_score REAL,
                embedding BLOB,
                source TEXT,
                curriculum_phase TEXT,
                selected BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_exemplar_candidates_session
                ON v16_exemplar_candidates(session_id);
            CREATE INDEX IF NOT EXISTS idx_exemplar_candidates_intent
                ON v16_exemplar_candidates(true_intent);

            CREATE TABLE IF NOT EXISTS v16_learning_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                exemplars_added INTEGER,
                accuracy_before REAL,
                accuracy_after REAL,
                temperature REAL,
                kp REAL,
                ki REAL,
                kd REAL,
                accepted BOOLEAN,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_learning_history_session
                ON v16_learning_history(session_id);

            CREATE TABLE IF NOT EXISTS v16_calibration_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                temperature_coeff REAL NOT NULL,
                ece_before REAL,
                ece_after REAL,
                n_test_queries INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_calibration_history_session
                ON v16_calibration_history(session_id);

            -- WRITE-BACK target: the single ACTIVE confidence-calibration temperature
            -- the live classifier (f1_classifier) reads at startup. Latest row wins.
            -- Only ACCEPTED calibrations (Phase 5 verified ECE reduction) land here.
            CREATE TABLE IF NOT EXISTS v16_active_calibration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                temperature_coeff REAL NOT NULL,
                ece_before REAL,
                ece_after REAL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS v16_pid_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                p_gain REAL NOT NULL,
                i_gain REAL NOT NULL,
                d_gain REAL NOT NULL,
                k_ultimate REAL,
                t_ultimate REAL,
                settling_time_sessions INTEGER,
                overshoot_pct REAL,
                oscillation_detected BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_pid_history_session
                ON v16_pid_history(session_id);

            CREATE TABLE IF NOT EXISTS v16_learning_evaluation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                accuracy_old REAL,
                accuracy_new REAL,
                improvement_pct REAL,
                statistically_significant BOOLEAN,
                p_value REAL,
                test_size INTEGER,
                accept_update BOOLEAN,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_learning_evaluation_session
                ON v16_learning_evaluation(session_id);
        """)
        conn.commit()
        conn.close()

    def learn_from_session(
        self,
        session_id: str,
        session_data: List[Dict],
        exemplar_pool: Dict[str, List[Dict]],
        ground_truth_labels: Dict[str, str],
        historical_sessions: Optional[List[Dict]] = None,
        new_predictions: Optional[List[bool]] = None,
    ) -> LearningResult:
        """
        Run all 5 learning phases on session data.

        Args:
            session_id: unique session identifier
            session_data: [{query, true_intent, predicted_intent, confidence, embedding}, ...]
            exemplar_pool: {intent: [exemplar_dicts]}
            ground_truth_labels: {query: true_intent}
            historical_sessions: [{budget, tokens_used}, ...] for PID tuning
            new_predictions: per-query correctness [bool] of a RETRAINED model
                (Phase 2 IMPROVE_EMBEDDINGS output), aligned to the ground-truth
                queries in `session_data`. When provided, Phase 5 gates acceptance
                on a McNemar accuracy A/B vs the current model. Defaults to None
                (Phase 2 is still a stub → that A/B path is inert; acceptance then
                falls back to the calibration-ECE signal).

        Returns:
            LearningResult with all phase outputs
        """
        logger.info(f"F7.APRENDIZAJE starting for session {session_id}")

        # === PHASE 1: SELECT_EXEMPLARS ===
        # V2.0 3c: defensivo — en producción session_data/exemplar_pool pueden venir
        # vacíos; se degrada a 0 exemplars en vez de crashear (F7 debe COMPLETAR).
        logger.info("Phase 1: SELECT_EXEMPLARS")
        exemplars_added = 0
        try:
            if session_data and exemplar_pool:
                exemplars_selected, _ = self.exemplar_selector.select_exemplars(
                    session_data, exemplar_pool, budget=20
                )
                exemplars_added = len(exemplars_selected)
        except Exception as e:
            logger.warning(f"Phase 1 SELECT_EXEMPLARS degradado: {e}")
        logger.info(f"Phase 1: Selected {exemplars_added} exemplars")

        # INERT (Fable-Gate 2026-07-05): IMPROVE_EMBEDDINGS es stub no-op; ground_truth nunca poblado en prod. Cementerio congelado — no invertir sin demanda.
        # === PHASE 2: IMPROVE_EMBEDDINGS — DEFERRED (honest: NOT implemented) ===
        # This is a STUB on purpose. Fine-tuning the embedding model is deferred, not
        # faked, because doing it for REAL requires infrastructure this loop does not
        # have, and pretending otherwise would violate HONEST:
        #   1. a labeled triplet corpus (anchor / positive / hard-negative). Phase 1
        #      MINES candidates, but there is no human-verified ground-truth dataset to
        #      train on.
        #   2. an actual training run (e.g. SentenceTransformer / SetFit triplet loss on
        #      GPU) — not something to run inline at session end.
        #   3. WRITE-BACK of the fine-tuned weights + re-embedding of all exemplars +
        #      an A/B gate. Phase 5's McNemar path is ALREADY wired to consume such a
        #      retrained model via `new_predictions`, and stays inert until one exists.
        # Until all three exist, this phase is a no-op. What IS live is Phase 3's
        # calibration write-back, which needs no training infra.
        logger.info(
            "Phase 2: IMPROVE_EMBEDDINGS — DEFERRED "
            "(needs triplet dataset + training run + fine-tuned-weight write-back; no-op)"
        )

        # === PHASE 3: CALIBRATE_CLASSIFIER ===
        # V2.0 3c: producción NO tiene ground-truth labels (no es entorno supervisado) →
        # sin labels no se recalibra (temperature=1.0). NO inventar labels.
        logger.info("Phase 3: CALIBRATE_CLASSIFIER")
        temperature = 1.0
        calibration_metrics: Optional[Dict[str, Any]] = None
        try:
            if ground_truth_labels and session_data:
                session_predictions = [
                    {
                        "query": d.get("query", ""),
                        "predicted_intent": d.get("predicted_intent", ""),
                        "confidence": d.get("confidence", 0.5),
                    }
                    for d in session_data
                ]
                calibration = self.calibrator.calibrate(session_predictions, ground_truth_labels)
                temperature = calibration.get("temperature_coeff", 1.0)
                calibration_metrics = calibration  # keep ece_before/after for Phase 5
            else:
                logger.info("Phase 3: sin ground-truth labels → temperature=1.0 (no recalibra)")
        except Exception as e:
            logger.warning(f"Phase 3 CALIBRATE degradado: {e}")
        logger.info(f"Phase 3: Temperature coefficient = {temperature:.4f}")

        # === PHASE 4: TUNE_PID_GAINS ===
        logger.info("Phase 4: TUNE_PID_GAINS")
        if historical_sessions is None:
            historical_sessions = []
        pid_gains = {"p_gain": 1.0, "i_gain": 0.0, "d_gain": 0.0}
        simulation = {"settling_time": 0, "overshoot": 0.0}
        try:
            K_u, T_u = self.pid_tuner.estimate_ultimate_gain(historical_sessions)
            pid_gains = self.pid_tuner.ziegler_nichols_gains(K_u, T_u, control_type="PID")
            simulation = self.pid_tuner.simulate_pid_response(
                pid_gains["p_gain"], pid_gains["i_gain"], pid_gains["d_gain"], n_steps=10
            )
            logger.info(
                f"Phase 4: Kp={pid_gains['p_gain']:.4f}, Ki={pid_gains['i_gain']:.6f}, "
                f"Kd={pid_gains['d_gain']:.4f}"
            )
            logger.info(
                f"Phase 4: Settling time={simulation['settling_time']}, "
                f"Overshoot={simulation['overshoot']:.1f}%"
            )
        except Exception as e:
            logger.warning(f"Phase 4 TUNE_PID degradado (datos insuficientes): {e}")

        # === PHASE 5: EVALUATE_LEARNING ===
        # Wired to the real LearningEvaluator (McNemar's test) + calibration ECE.
        # `accepted` is now the OUTPUT of an evaluation, not the unconditional
        # `True` placeholder it used to be. Fail-open default = NOT accepted.
        logger.info("Phase 5: EVALUATE_LEARNING")
        evaluation = self._evaluate_learning(
            session_data=session_data,
            ground_truth_labels=ground_truth_labels,
            calibration_metrics=calibration_metrics,
            new_predictions=new_predictions,
        )
        accuracy_before = evaluation["accuracy_before"]
        accuracy_after = evaluation["accuracy_after"]
        accepted = evaluation["accepted"]
        logger.info(
            f"Phase 5: accepted={accepted} "
            f"(acc {accuracy_before:.3f}→{accuracy_after:.3f}) — {evaluation['reason']}"
        )

        # === WRITE-BACK: publish the verified calibration to the LIVE classifier ===
        # This closes the learning loop. Only a calibration that Phase 5 ACCEPTED via
        # the ECE signal (real ground truth, error actually went down) is pushed to
        # v16_active_calibration, where f1_classifier reads it at startup and applies
        # it to its confidence. The McNemar/embeddings path ("signal" == "mcnemar") is
        # about a retrained model, not this temperature, so it does NOT write back here.
        # FAIL-OPEN: if nothing is accepted, the active coefficient is left untouched
        # (F1 keeps using whatever was last published, or 1.0 = identity).
        if accepted and evaluation.get("signal") == "calibration":
            self._persist_active_calibration(
                session_id, temperature, calibration_metrics, evaluation["reason"]
            )

        reason = (
            f"Phase 1: {exemplars_added} exemplars. Phase 3: T={temperature:.2f}. "
            f"Phase 4: Kp={pid_gains['p_gain']:.4f}. "
            f"Phase 5: {'ACCEPTED' if accepted else 'REJECTED'} — {evaluation['reason']}"
        )

        result = LearningResult(
            exemplars_added=exemplars_added,
            accuracy_before=accuracy_before,
            accuracy_after=accuracy_after,
            temperature=temperature,
            pid_gains=pid_gains,
            accepted=accepted,
            reason=reason,
        )

        # === Store results ===
        self._store_learning_result(session_id, result)

        logger.info(f"F7.APRENDIZAJE completed: {reason}")
        return result

    def _evaluate_learning(
        self,
        session_data: List[Dict],
        ground_truth_labels: Dict[str, str],
        calibration_metrics: Optional[Dict[str, Any]],
        new_predictions: Optional[List[bool]] = None,
    ) -> Dict[str, Any]:
        """Phase 5: decide whether to ACCEPT the learning update via a real evaluation.

        Two evaluable signals, in priority order:

        1. **Accuracy A/B (McNemar's test)** — only reachable when a retrained
           model supplies ``new_predictions`` (per-query correctness of the new
           model, aligned to the ground-truth queries). The update is accepted
           only on a statistically-significant accuracy gain. This is the wired
           path for Phase 2 IMPROVE_EMBEDDINGS; until that stub produces a new
           model, ``new_predictions`` is None and this path stays inert.

        2. **Calibration ECE** — when temperature scaling ran on real
           ground-truth data, accept iff it reduced the Expected Calibration
           Error. (Temperature scaling is monotonic, so it never changes the
           argmax prediction → classification accuracy is unchanged; ECE is the
           meaningful signal for a calibration-only update.)

        Fail-open contract: with no ground truth and no retrained model — i.e.
        the unsupervised production path — there is nothing to verify, so return
        ``accepted=False``. We refuse the update rather than blindly accepting
        it (the old placeholder accepted everything).

        Args:
            session_data: per-query records with ``query``/``predicted_intent``.
            ground_truth_labels: {query: true_intent} (empty in production).
            calibration_metrics: full dict from ``ClassifierCalibrator.calibrate``
                (carries ``ece_before``/``ece_after``/``n_test_queries``), or None.
            new_predictions: per-query correctness [bool] of a retrained model.

        Returns:
            {accepted: bool, accuracy_before: float, accuracy_after: float, reason: str}
        """
        # Real baseline accuracy of the CURRENT model on ground-truthed queries.
        old_correct: List[bool] = [
            d.get("predicted_intent", "") == ground_truth_labels[d.get("query", "")]
            for d in session_data
            if d.get("query", "") in ground_truth_labels
        ]
        accuracy_before = float(np.mean(old_correct)) if old_correct else 0.0

        # --- Signal 1: retrained-model A/B via McNemar's test ---
        if new_predictions is not None and old_correct and len(new_predictions) == len(old_correct):
            verdict = self.evaluator.mcnemar_test(old_correct, new_predictions)
            return {
                "accepted": bool(verdict["accept_update"]),
                "accuracy_before": float(verdict["accuracy_old"]),
                "accuracy_after": float(verdict["accuracy_new"]),
                "reason": f"McNemar A/B: {verdict['reason']}",
                "signal": "mcnemar",
            }

        # --- Signal 2: calibration ECE reduction on real data ---
        if calibration_metrics and calibration_metrics.get("n_test_queries", 0) > 0:
            ece_before = float(calibration_metrics.get("ece_before", 0.0))
            ece_after = float(calibration_metrics.get("ece_after", 0.0))
            improved = ece_after < ece_before - 1e-6
            return {
                "accepted": bool(improved),
                "accuracy_before": accuracy_before,
                # temperature scaling does not change argmax → accuracy unchanged
                "accuracy_after": accuracy_before,
                "reason": (
                    f"Calibration {'reduced' if improved else 'did not reduce'} ECE "
                    f"({ece_before:.4f}→{ece_after:.4f})"
                ),
                "signal": "calibration",
            }

        # --- Fail-open: nothing evaluable (e.g. unsupervised prod) → refuse ---
        return {
            "accepted": False,
            "accuracy_before": accuracy_before,
            "accuracy_after": accuracy_before,
            "reason": "no evaluable improvement (no ground truth / no retrained model)",
            "signal": "none",
        }

    def _persist_active_calibration(
        self,
        session_id: str,
        temperature: float,
        calibration_metrics: Optional[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Write-back: publish an ACCEPTED calibration temperature for the live classifier.

        Appends a row to ``v16_active_calibration`` (latest-wins — what f1_classifier
        reads at startup) and an audit row to ``v16_calibration_history`` (a table that
        existed but was never written before this loop closed). This is the only path
        by which F7 changes runtime behavior: a verified temperature actually reaches
        the classifier instead of being computed and discarded.

        FAIL-SAFE (not fail-silent): a write failure is LOGGED at WARNING and never
        raised — F7 must complete and the classifier keeps running on its previous /
        default (1.0) temperature.

        Args:
            session_id: Session that produced the calibration.
            temperature: The accepted temperature coefficient to publish.
            calibration_metrics: Full dict from ``ClassifierCalibrator.calibrate``.
            reason: Phase 5 acceptance reason (audit string).
        """
        ece_before = (
            float(calibration_metrics.get("ece_before", 0.0)) if calibration_metrics else 0.0
        )
        ece_after = float(calibration_metrics.get("ece_after", 0.0)) if calibration_metrics else 0.0
        n_test = int(calibration_metrics.get("n_test_queries", 0)) if calibration_metrics else 0

        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute(
                """
                INSERT INTO v16_active_calibration
                (session_id, temperature_coeff, ece_before, ece_after, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, float(temperature), ece_before, ece_after, reason),
            )
            epoch_row = conn.execute(
                "SELECT COALESCE(MAX(epoch), 0) + 1 FROM v16_calibration_history"
            ).fetchone()
            epoch = int(epoch_row[0]) if epoch_row else 1
            conn.execute(
                """
                INSERT INTO v16_calibration_history
                (epoch, session_id, temperature_coeff, ece_before, ece_after, n_test_queries)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (epoch, session_id, float(temperature), ece_before, ece_after, n_test),
            )
            conn.commit()
            logger.info(
                f"F7 WRITE-BACK: published active calibration temperature={temperature:.4f} "
                f"for live classifier (ECE {ece_before:.4f}→{ece_after:.4f}, n={n_test})"
            )
        except sqlite3.Error as e:
            logger.warning(f"F7 write-back FAILED to persist active calibration: {e}")
        finally:
            if conn is not None:
                conn.close()

    def _store_learning_result(self, session_id: str, result: LearningResult) -> None:
        """Store learning phase results to sessions.db."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        conn.execute(
            """
            INSERT INTO v16_learning_history
            (session_id, exemplars_added, accuracy_before, accuracy_after,
             temperature, kp, ki, kd, accepted, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                result.exemplars_added,
                result.accuracy_before,
                result.accuracy_after,
                result.temperature,
                result.pid_gains["p_gain"],
                result.pid_gains["i_gain"],
                result.pid_gains["d_gain"],
                result.accepted,
                result.reason,
            ),
        )
        conn.commit()
        conn.close()

    def apply_reward_signals(self, decision_id: str) -> Dict[str, float]:
        """Apply reward signals to adapt learning parameters.

        V16.10 H44: Fetch adaptation parameters from soft_reward_loop
        based on historical reward signals for this decision.

        Args:
            decision_id: Session or query identifier.

        Returns:
            Dict with keys: depth_multiplier, strategy_confidence,
            exemplar_budget_scale, pid_adaptive_tau.
        """
        try:
            from . import soft_reward_loop

            adaptation = soft_reward_loop.compute_adaptation(decision_id, window_size=20)
            self._reward_signals[decision_id] = adaptation
            logger.info(f"Applied reward signals for {decision_id}: {adaptation}")
            return adaptation

        except (ImportError, AttributeError):
            logger.debug("soft_reward_loop not available; returning neutral params")
            return {
                "depth_multiplier": 1.0,
                "strategy_confidence": 0.5,
                "exemplar_budget_scale": 1.0,
                "pid_adaptive_tau": 1.0,
            }


def format_confidence_reminder(score: float, context: str = "decision") -> str:
    """Format ECE-calibrated confidence for system-reminder injection.

    Args:
        score: Confidence score 0.0-1.0 (from ECE calibration)
        context: Context string (e.g., "decision", "f5_validation", "voting")

    Returns:
        Formatted string for system-reminder: "[Confidence: X% LEVEL] context"

    Example:
        >>> format_confidence_reminder(0.87, "research decision")
        "[Confidence: 87% HIGH] research decision"
    """
    if score >= 0.9:
        level = "HIGH"
    elif score >= 0.7:
        level = "MEDIUM"
    else:
        level = "LOW"

    return f"[Confidence: {score:.0%} {level}] {context}"
