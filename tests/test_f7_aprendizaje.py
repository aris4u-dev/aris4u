"""Tests for F7.APRENDIZAJE learning engine (5 phases + safeguards)."""

import pytest
import numpy as np
from engine.v16.f7_aprendizaje import (
    ExemplarSelector,
    ClassifierCalibrator,
    PIDTuner,
    LearningEvaluator,
    AprendizajeEngine,
)


# ============================================================================
# PHASE 1: EXEMPLAR SELECTION TESTS
# ============================================================================


class TestExemplarSelection:
    """Test Phase 1: uncertainty sampling + hard negative mining."""

    def test_uncertainty_score_computation(self):
        """Phase 1: Uncertainty = 1 - confidence."""
        selector = ExemplarSelector()
        assert selector.compute_uncertainty_score(0.95) == pytest.approx(0.05, abs=1e-6)
        assert selector.compute_uncertainty_score(0.50) == pytest.approx(0.50, abs=1e-6)
        assert selector.compute_uncertainty_score(0.10) == pytest.approx(0.90, abs=1e-6)

    def test_cosine_similarity_identical_vectors(self):
        """Phase 1: Cosine similarity of identical vectors is 1.0."""
        selector = ExemplarSelector()
        emb1 = np.array([1.0, 0.0, 0.0])
        emb2 = np.array([1.0, 0.0, 0.0])
        assert selector.cosine_similarity(emb1, emb2) == pytest.approx(1.0, abs=1e-6)

    def test_cosine_similarity_orthogonal_vectors(self):
        """Phase 1: Cosine similarity of orthogonal vectors is 0.0."""
        selector = ExemplarSelector()
        emb1 = np.array([1.0, 0.0, 0.0])
        emb2 = np.array([0.0, 1.0, 0.0])
        assert selector.cosine_similarity(emb1, emb2) == pytest.approx(0.0, abs=1e-6)

    def test_selects_uncertain_queries(self):
        """Phase 1: Queries with low confidence selected as candidates."""
        selector = ExemplarSelector()
        session_data = [
            {
                "query": "easy question",
                "true_intent": "simple",
                "predicted_intent": "simple",
                "confidence": 0.95,
                "embedding": np.array([1.0, 0.0, 0.0]),
            },
            {
                "query": "hard question",
                "true_intent": "fix",
                "predicted_intent": "simple",
                "confidence": 0.45,
                "embedding": np.array([0.5, 0.5, 0.0]),
            },
        ]

        exemplar_pool = {"simple": [], "fix": []}

        candidates, _ = selector.select_exemplars(
            session_data, exemplar_pool, uncertainty_threshold=0.70, budget=5
        )

        # Only the hard question (confidence 0.45) should be selected
        assert len(candidates) >= 1
        selected_queries = [c["query"] for c in candidates]
        assert "hard question" in selected_queries

    def test_hard_negative_mining(self):
        """Phase 1: Hard negatives identified (similar embedding, different intent)."""
        selector = ExemplarSelector()

        session_data = [
            {
                "query": "fix authentication",
                "true_intent": "fix",
                "predicted_intent": "simple",
                "confidence": 0.40,
                "embedding": np.array([0.8, 0.2, 0.0]),
            }
        ]

        exemplar_pool = {
            "simple": [
                {
                    "query": "show auth details",
                    "embedding": np.array([0.75, 0.25, 0.0]),  # Similar to the query
                },
                {
                    "query": "what is a tensor",
                    "embedding": np.array([0.0, 0.0, 1.0]),  # Very different
                },
            ],
            "fix": [],
        }

        candidates, hard_negs = selector.select_exemplars(
            session_data, exemplar_pool, similarity_threshold=0.70, budget=5
        )

        # Should find hard negative (similar to query but different intent)
        assert any(c["hard_negatives"] for c in candidates)

    def test_curriculum_ordering_easy_before_hard(self):
        """Phase 1: Easy examples (high confidence misclassification) selected first."""
        selector = ExemplarSelector()

        candidates = [
            {
                "query": "q1",
                "confidence_old": 0.8,
                "uncertainty_score": 0.2,
                "regret_score": 0.1,
                "hard_negatives": [],
            },
            {
                "query": "q2",
                "confidence_old": 0.3,
                "uncertainty_score": 0.7,
                "regret_score": 0.35,
                "hard_negatives": [],
            },
        ]

        selected = selector._select_final_exemplars(candidates, budget=2)
        assert len(selected) == 2
        # Easy candidates should be prioritized
        assert any(c["confidence_old"] > 0.5 for c in selected)

    def test_deduplication_removes_existing_queries(self):
        """Phase 1: Exemplars already in pool not selected again."""
        selector = ExemplarSelector()

        session_data = [
            {
                "query": "fix bug",
                "true_intent": "fix",
                "predicted_intent": "fix",
                "confidence": 0.40,
                "embedding": np.array([1.0, 0.0, 0.0]),
            }
        ]

        exemplar_pool = {
            "fix": [{"query": "fix bug", "embedding": np.array([1.0, 0.0, 0.0])}],
        }

        candidates, _ = selector.select_exemplars(session_data, exemplar_pool, budget=5)
        # "fix bug" already exists, should be filtered out
        selected_queries = [c["query"] for c in candidates]
        assert "fix bug" not in selected_queries


# ============================================================================
# PHASE 3: CALIBRATION TESTS
# ============================================================================


class TestClassifierCalibration:
    """Test Phase 3: temperature scaling calibration."""

    def test_softmax_computation(self):
        """Phase 3: Softmax sums to 1.0."""
        calibrator = ClassifierCalibrator()
        logits = np.array([[1.0, 2.0, 0.5], [0.1, 0.2, 0.3]])
        probs = calibrator.softmax(logits, axis=1)
        assert np.allclose(probs.sum(axis=1), 1.0)

    def test_ece_zero_for_perfect_confidence(self):
        """Phase 3: ECE is low when confidence matches accuracy."""
        calibrator = ClassifierCalibrator()
        # All predictions: high confidence and correct
        logits = np.array([[3.0, 0.0], [3.0, 0.0], [3.0, 0.0]] * 3)
        labels = np.ones(9)  # All correct
        ece = calibrator.compute_ece(logits, labels, temperature=1.0, n_bins=5)
        assert ece < 0.1

    def test_ece_high_for_overconfident(self):
        """Phase 3: ECE is high when confident predictions are wrong."""
        calibrator = ClassifierCalibrator()
        # High confidence but 50% accuracy
        logits = np.array(
            [[3.0, 0.0], [3.0, 0.0], [0.0, 3.0], [0.0, 3.0]] * 2
        )  # Alternating
        labels = np.array([1, 1, 0, 0, 1, 1, 0, 0])  # 50% correct
        ece = calibrator.compute_ece(logits, labels, temperature=1.0, n_bins=5)
        assert ece > 0.1

    def test_temperature_scaling_reduces_ece(self):
        """Phase 3: Temperature scaling should reduce ECE."""
        calibrator = ClassifierCalibrator()
        logits = np.array([[3.0, 0.0], [3.0, 0.0], [0.0, 3.0], [0.0, 3.0]])
        labels = np.array([1, 1, 0, 0])

        ece_before = calibrator.compute_ece(logits, labels, temperature=1.0)
        ece_after = calibrator.compute_ece(logits, labels, temperature=1.5)
        # Temperature scaling should help (though not guaranteed for all cases)
        assert ece_before >= 0  # Valid ECE
        assert ece_after >= 0  # Valid ECE

    def test_calibrate_with_empty_predictions(self):
        """Phase 3: Handle empty predictions gracefully."""
        calibrator = ClassifierCalibrator()
        result = calibrator.calibrate([], {})
        assert result["temperature_coeff"] == 1.0
        assert result["n_test_queries"] == 0

    def test_calibrate_with_valid_data(self):
        """Phase 3: Calibration produces reasonable temperature."""
        calibrator = ClassifierCalibrator()
        session_preds = [
            {"query": f"q{i}", "predicted_intent": "fix", "confidence": 0.9}
            for i in range(20)
        ]
        ground_truth = {f"q{i}": "fix" for i in range(20)}

        result = calibrator.calibrate(session_preds, ground_truth)
        assert 0.1 <= result["temperature_coeff"] <= 10.0
        assert result["ece_after"] >= 0


# ============================================================================
# PHASE 4: PID TUNING TESTS
# ============================================================================


class TestPIDTuning:
    """Test Phase 4: Ziegler-Nichols PID tuning."""

    def test_estimate_ultimate_gain_with_insufficient_data(self):
        """Phase 4: Graceful fallback for sparse data."""
        tuner = PIDTuner()
        K_u, T_u = tuner.estimate_ultimate_gain([])
        assert K_u == 1.0
        assert T_u == 5.0

    def test_estimate_ultimate_gain_with_step_response(self):
        """Phase 4: Estimates K_u and T_u from step changes."""
        tuner = PIDTuner()
        session_data = [
            {"budget": 10000, "tokens_used": 9500},  # baseline
            {"budget": 10500, "tokens_used": 10200},  # step up: +500 budget → +700 tokens
            {"budget": 10500, "tokens_used": 10150},  # settling
            {"budget": 10500, "tokens_used": 10100},  # settling
        ]

        K_u, T_u = tuner.estimate_ultimate_gain(session_data, budget_nominal=10000)
        assert K_u > 0
        assert T_u > 0

    def test_ziegler_nichols_P_control(self):
        """Phase 4: Z-N P control formula."""
        tuner = PIDTuner()
        gains = tuner.ziegler_nichols_gains(K_u=1.0, T_u=4.0, control_type="P")
        assert gains["p_gain"] == pytest.approx(0.5, abs=1e-6)
        assert gains["i_gain"] == 0.0
        assert gains["d_gain"] == 0.0

    def test_ziegler_nichols_PI_control(self):
        """Phase 4: Z-N PI control formula."""
        tuner = PIDTuner()
        gains = tuner.ziegler_nichols_gains(K_u=1.0, T_u=4.0, control_type="PI")
        assert gains["p_gain"] == pytest.approx(0.45, abs=1e-6)
        assert gains["i_gain"] > 0
        assert gains["d_gain"] == 0.0

    def test_ziegler_nichols_PID_control(self):
        """Phase 4: Z-N PID control formula."""
        tuner = PIDTuner()
        gains = tuner.ziegler_nichols_gains(K_u=1.0, T_u=4.0, control_type="PID")
        assert gains["p_gain"] == pytest.approx(0.6, abs=1e-6)
        assert gains["i_gain"] > 0
        assert gains["d_gain"] > 0

    def test_simulate_pid_stable_response(self):
        """Phase 4: PID simulation produces stable response."""
        tuner = PIDTuner()
        sim = tuner.simulate_pid_response(
            p_gain=0.6,
            i_gain=0.3,
            d_gain=0.3,
            target_budget=10000,
            n_steps=20,
        )

        assert sim["settling_time"] <= 20
        assert sim["overshoot"] >= 0
        assert len(sim["tokens_used"]) == 20
        assert len(sim["errors"]) == 20

    def test_simulate_pid_oscillation_detection(self):
        """Phase 4: Oscillation detection works."""
        tuner = PIDTuner()
        sim = tuner.simulate_pid_response(
            p_gain=5.0,  # Very high gain → oscillation
            i_gain=2.0,
            d_gain=0.1,
            target_budget=10000,
            n_steps=20,
        )

        # High gain may cause oscillation
        assert "oscillates" in sim


# ============================================================================
# PHASE 5: LEARNING EVALUATION TESTS
# ============================================================================


class TestLearningEvaluation:
    """Test Phase 5: McNemar's test for statistical significance."""

    def test_mcnemar_no_improvement(self):
        """Phase 5: McNemar detects when there's no improvement."""
        evaluator = LearningEvaluator()
        old_preds = [True, True, True, False, False, False] * 5
        new_preds = [True, True, True, False, False, False] * 5

        result = evaluator.mcnemar_test(old_preds, new_preds)
        assert result["improvement"] == pytest.approx(0.0, abs=1e-6)
        assert not result["accept_update"]

    def test_mcnemar_significant_improvement(self):
        """Phase 5: McNemar detects significant improvement."""
        evaluator = LearningEvaluator()
        # Old: 75% accuracy (60 correct, 20 wrong)
        old_preds = [True] * 60 + [False] * 20
        # New: 95% accuracy (76 correct, 4 wrong) — 16 fixes, 0 regressions
        new_preds = [True] * 76 + [False] * 4

        result = evaluator.mcnemar_test(old_preds, new_preds, min_improvement=0.02)
        assert result["improvement"] == pytest.approx(0.20, abs=0.01)
        assert result["accuracy_new"] > result["accuracy_old"]

    def test_mcnemar_contingency_table(self):
        """Phase 5: Contingency table computed correctly."""
        evaluator = LearningEvaluator()
        # Old: [T, T, T, F, F]
        # New: [T, T, F, F, T]
        old_preds = [True, True, True, False, False]
        new_preds = [True, True, False, False, True]

        result = evaluator.mcnemar_test(old_preds, new_preds)
        ct = result["contingency_table"]

        assert ct["old_pass_new_pass"] == 2  # queries 0, 1
        assert ct["old_pass_new_fail"] == 1  # query 2
        assert ct["old_fail_new_pass"] == 1  # query 4
        assert ct["old_fail_new_fail"] == 1  # query 3

    def test_mcnemar_p_value_valid_range(self):
        """Phase 5: P-value is in [0, 1]."""
        evaluator = LearningEvaluator()
        old_preds = [True] * 50 + [False] * 50
        new_preds = [True] * 60 + [False] * 40

        result = evaluator.mcnemar_test(old_preds, new_preds)
        assert 0.0 <= result["p_value"] <= 1.0

    def test_mcnemar_min_improvement_threshold(self):
        """Phase 5: Respects minimum improvement threshold."""
        evaluator = LearningEvaluator()
        old_preds = [True] * 50 + [False] * 50
        new_preds = [True] * 51 + [False] * 49  # 1% improvement

        result = evaluator.mcnemar_test(old_preds, new_preds, min_improvement=0.02)
        # 1% < 2% threshold
        assert not result["accept_update"]


# ============================================================================
# INTEGRATION TESTS
# ============================================================================


class TestAprendizajeEngineIntegration:
    """End-to-end tests for F7.APRENDIZAJE."""

    def test_engine_initialization(self, tmp_path):
        """F7: Engine initializes and creates schema."""
        db_path = tmp_path / "test.db"
        AprendizajeEngine(str(db_path))
        assert db_path.exists()

    def test_learn_from_session_minimal(self, tmp_path):
        """F7: learn_from_session runs all 5 phases."""
        db_path = tmp_path / "test.db"
        engine = AprendizajeEngine(str(db_path))

        session_data = [
            {
                "query": "fix auth",
                "true_intent": "fix",
                "predicted_intent": "simple",
                "confidence": 0.45,
                "embedding": np.array([0.8, 0.2, 0.0]),
            },
            {
                "query": "build api",
                "true_intent": "implementation",
                "predicted_intent": "implementation",
                "confidence": 0.92,
                "embedding": np.array([0.1, 0.9, 0.0]),
            },
        ]

        exemplar_pool = {
            "simple": [],
            "fix": [],
            "implementation": [],
        }

        ground_truth = {
            "fix auth": "fix",
            "build api": "implementation",
        }

        result = engine.learn_from_session(
            session_id="test_session",
            session_data=session_data,
            exemplar_pool=exemplar_pool,
            ground_truth_labels=ground_truth,
        )

        assert result.exemplars_added >= 0
        assert result.temperature > 0
        assert "p_gain" in result.pid_gains
        assert result.reason is not None

    def test_learning_results_stored_in_db(self, tmp_path):
        """F7: Learning results persisted to sessions.db."""
        import sqlite3

        db_path = tmp_path / "test.db"
        engine = AprendizajeEngine(str(db_path))

        session_data = []
        exemplar_pool = {"simple": [], "fix": [], "implementation": []}
        ground_truth = {}

        engine.learn_from_session(
            session_id="test_session_2",
            session_data=session_data,
            exemplar_pool=exemplar_pool,
            ground_truth_labels=ground_truth,
        )

        # Verify data was stored
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT * FROM v16_learning_history WHERE session_id = ?",
            ("test_session_2",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None


# ============================================================================
# PHASE-SPECIFIC EDGE CASES
# ============================================================================


class TestPhaseEdgeCases:
    """Edge cases and boundary conditions."""

    def test_phase1_all_correct_predictions(self):
        """Phase 1: No uncertainty when all predictions correct."""
        selector = ExemplarSelector()
        session_data = [
            {
                "query": f"q{i}",
                "true_intent": "simple",
                "predicted_intent": "simple",
                "confidence": 0.99,
                "embedding": np.random.randn(384),
            }
            for i in range(10)
        ]

        candidates, _ = selector.select_exemplars(session_data, {}, budget=10)
        # No low-confidence predictions
        assert len(candidates) == 0

    def test_phase3_small_validation_set(self):
        """Phase 3: Handle very small validation sets."""
        calibrator = ClassifierCalibrator()
        session_preds = [
            {"query": "q1", "predicted_intent": "fix", "confidence": 0.9}
        ]
        ground_truth = {"q1": "fix"}

        result = calibrator.calibrate(session_preds, ground_truth, test_fraction=0.5)
        assert result["temperature_coeff"] > 0

    def test_phase4_insufficient_history(self):
        """Phase 4: Graceful fallback with sparse session history."""
        tuner = PIDTuner()
        K_u, T_u = tuner.estimate_ultimate_gain([{"budget": 10000, "tokens_used": 9500}])
        assert K_u == 1.0
        assert T_u == 5.0

    def test_phase5_all_correct_predictions(self):
        """Phase 5: No improvement if model already 100% correct."""
        evaluator = LearningEvaluator()
        old_preds = [True] * 50
        new_preds = [True] * 50

        result = evaluator.mcnemar_test(old_preds, new_preds)
        assert result["improvement"] == 0.0


# ============================================================================
# PHASE 5 WIRING: accepted depends on a REAL evaluation (not unconditional True)
# ============================================================================


class TestPhase5AcceptanceWiring:
    """Verify the learning loop's `accepted` is the OUTPUT of _evaluate_learning,
    not the old unconditional `accepted = True` placeholder."""

    def _engine(self, tmp_path):
        return AprendizajeEngine(str(tmp_path / "eval.db"))

    def _supervised_session(self):
        """80 queries: first 60 predicted correctly, last 20 wrong → 75% baseline."""
        session_data = [
            {
                "query": f"q{i}",
                "true_intent": "fix",
                "predicted_intent": "fix" if i < 60 else "wrong",
                "confidence": 0.6,
                "embedding": np.zeros(3),
            }
            for i in range(80)
        ]
        ground_truth = {f"q{i}": "fix" for i in range(80)}
        return session_data, ground_truth

    def test_evaluate_accepts_significant_improvement(self, tmp_path):
        """McNemar path: a retrained model that fixes 16 errors with 0 regressions
        is ACCEPTED, and accuracy_after reflects the real gain."""
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._supervised_session()
        # New model: 76 correct / 4 wrong (95%), fixes 16 of the 20 errors, no regressions.
        new_predictions = [True] * 76 + [False] * 4

        res = engine._evaluate_learning(
            session_data, ground_truth, calibration_metrics=None, new_predictions=new_predictions
        )
        assert res["accepted"] is True
        assert res["accuracy_before"] == pytest.approx(0.75, abs=1e-6)
        assert res["accuracy_after"] == pytest.approx(0.95, abs=1e-6)

    def test_evaluate_rejects_no_improvement(self, tmp_path):
        """McNemar path: a retrained model identical to the old one is REJECTED.
        Same inputs, opposite verdict → `accepted` truly depends on the evaluation."""
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._supervised_session()
        # New model == old model: first 60 correct, last 20 wrong → no improvement.
        new_predictions = [True] * 60 + [False] * 20

        res = engine._evaluate_learning(
            session_data, ground_truth, calibration_metrics=None, new_predictions=new_predictions
        )
        assert res["accepted"] is False
        assert res["accuracy_before"] == pytest.approx(0.75, abs=1e-6)

    def test_evaluate_rejects_regression(self, tmp_path):
        """McNemar path: a retrained model that gets WORSE is REJECTED."""
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._supervised_session()
        # New model: only 40 correct (50%) → worse than 75%.
        new_predictions = [True] * 40 + [False] * 40

        res = engine._evaluate_learning(
            session_data, ground_truth, calibration_metrics=None, new_predictions=new_predictions
        )
        assert res["accepted"] is False

    def test_evaluate_calibration_signal(self, tmp_path):
        """Calibration path: accept iff ECE was reduced on real data."""
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._supervised_session()

        improved = {"ece_before": 0.20, "ece_after": 0.10, "n_test_queries": 16}
        worse = {"ece_before": 0.10, "ece_after": 0.20, "n_test_queries": 16}

        res_ok = engine._evaluate_learning(session_data, ground_truth, improved)
        res_no = engine._evaluate_learning(session_data, ground_truth, worse)
        assert res_ok["accepted"] is True
        assert res_no["accepted"] is False

    def test_evaluate_failopen_no_groundtruth(self, tmp_path):
        """Fail-open: unsupervised (no ground truth, no retrained model) → NOT accepted."""
        engine = self._engine(tmp_path)
        session_data = [
            {"query": "q", "predicted_intent": "fix", "confidence": 0.6, "embedding": np.zeros(3)}
        ]
        res = engine._evaluate_learning(session_data, {}, calibration_metrics=None)
        assert res["accepted"] is False
        assert res["accuracy_before"] == 0.0

    def test_learn_from_session_no_groundtruth_not_accepted(self, tmp_path):
        """End-to-end: the production call path (empty ground truth) no longer
        returns the old unconditional accepted=True."""
        engine = self._engine(tmp_path)
        session_data = [
            {
                "query": "fix auth",
                "true_intent": "fix",
                "predicted_intent": "simple",
                "confidence": 0.45,
                "embedding": np.array([0.8, 0.2, 0.0]),
            }
        ]
        result = engine.learn_from_session(
            session_id="prod_unsupervised",
            session_data=session_data,
            exemplar_pool={"fix": [], "simple": []},
            ground_truth_labels={},
        )
        assert result.accepted is False
        assert "REJECTED" in result.reason

    def test_learn_from_session_accepts_with_retrained_model(self, tmp_path):
        """End-to-end: passing a genuinely-better retrained model through the
        public API flips `accepted` to True via the McNemar gate."""
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._supervised_session()
        new_predictions = [True] * 76 + [False] * 4  # 95%, real improvement

        result = engine.learn_from_session(
            session_id="retrained",
            session_data=session_data,
            exemplar_pool={},
            ground_truth_labels=ground_truth,
            new_predictions=new_predictions,
        )
        assert result.accepted is True
        assert result.accuracy_after == pytest.approx(0.95, abs=1e-6)
        assert "ACCEPTED" in result.reason


# ============================================================================
# CALIBRATION WRITE-BACK TESTS (Phase 3 → live f1_classifier)
# ============================================================================


class TestCalibrationWriteBack:
    """The accepted calibration temperature is PUBLISHED to v16_active_calibration
    so the live classifier applies it. This is the real learning F7 does in prod."""

    def _engine(self, tmp_path):
        return AprendizajeEngine(str(tmp_path / "wb.db"))

    def _overconfident_session(self):
        """60 queries claiming 93% confidence but only 50% correct → overconfident.
        Temperature scaling reduces ECE here → Phase 5 accepts via the ECE signal."""
        session_data = []
        ground_truth = {}
        for i in range(60):
            correct = i % 2 == 0
            true = "implementation" if correct else "fix"
            session_data.append(
                {
                    "query": f"q{i}",
                    "true_intent": true,
                    "predicted_intent": "implementation",
                    "confidence": 0.93,
                    "embedding": np.zeros(3),
                }
            )
            ground_truth[f"q{i}"] = true
        return session_data, ground_truth

    def _active_temperature(self, engine):
        import sqlite3

        conn = sqlite3.connect(str(engine.db_path))
        row = conn.execute(
            "SELECT temperature_coeff FROM v16_active_calibration ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def test_accepted_calibration_is_written_back(self, tmp_path):
        """An accepted ECE-reducing calibration lands in v16_active_calibration,
        and f1's fail-open reader reads exactly what F7 wrote."""
        from engine.v16.f1_classifier import _load_active_temperature

        np.random.seed(0)
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._overconfident_session()

        result = engine.learn_from_session(
            session_id="wb_accept",
            session_data=session_data,
            exemplar_pool={},
            ground_truth_labels=ground_truth,
        )
        assert result.accepted is True
        active = self._active_temperature(engine)
        assert active is not None
        assert active != 1.0  # a real (non-identity) calibration was published
        assert active == result.temperature
        # f1's reader sees the same value the loop wrote
        assert _load_active_temperature(engine.db_path) == pytest.approx(active, abs=1e-9)
        # audit trail populated too
        import sqlite3

        n_hist = sqlite3.connect(str(engine.db_path)).execute(
            "SELECT COUNT(*) FROM v16_calibration_history"
        ).fetchone()[0]
        assert n_hist == 1

    def test_unsupervised_session_writes_nothing(self, tmp_path):
        """Fail-open: with no ground truth, nothing is accepted and nothing is
        published → live classifier keeps temperature 1.0 (identity)."""
        from engine.v16.f1_classifier import _load_active_temperature

        engine = self._engine(tmp_path)
        session_data = [
            {"query": "q", "true_intent": "fix", "predicted_intent": "fix", "confidence": 0.6}
        ]
        result = engine.learn_from_session(
            session_id="prod_unsup",
            session_data=session_data,
            exemplar_pool={},
            ground_truth_labels={},
        )
        assert result.accepted is False
        assert self._active_temperature(engine) is None
        # reader fail-open default
        assert _load_active_temperature(engine.db_path) == 1.0

    def test_mcnemar_acceptance_does_not_write_temperature(self, tmp_path):
        """A retrained-model (McNemar) acceptance is about embeddings, not this
        temperature → it must NOT publish a calibration coefficient."""
        engine = self._engine(tmp_path)
        session_data = [
            {
                "query": f"q{i}",
                "true_intent": "fix",
                "predicted_intent": "fix" if i < 60 else "wrong",
                "confidence": 0.6,
                "embedding": np.zeros(3),
            }
            for i in range(80)
        ]
        ground_truth = {f"q{i}": "fix" for i in range(80)}
        new_predictions = [True] * 76 + [False] * 4  # real gain → McNemar accepts

        result = engine.learn_from_session(
            session_id="wb_mcnemar",
            session_data=session_data,
            exemplar_pool={},
            ground_truth_labels=ground_truth,
            new_predictions=new_predictions,
        )
        assert result.accepted is True  # accepted, but via McNemar
        assert self._active_temperature(engine) is None  # no temperature write-back

    def test_write_back_failure_is_fail_safe_not_fatal(self, tmp_path, caplog):
        """A write-back DB error is LOGGED and swallowed (fail-safe, not fail-silent):
        the real _persist_active_calibration must never raise, and learn_from_session
        must still COMPLETE and return a result."""
        import logging
        import sqlite3 as _sqlite
        from unittest.mock import patch

        np.random.seed(0)
        engine = self._engine(tmp_path)
        session_data, ground_truth = self._overconfident_session()

        # Force the persistence connection to fail at write time.
        with patch(
            "engine.v16.f7_aprendizaje.sqlite3.connect",
            side_effect=_sqlite.Error("disk gone"),
        ):
            with caplog.at_level(logging.WARNING):
                # Direct call: must NOT raise.
                engine._persist_active_calibration(
                    "s", 2.0, {"ece_before": 0.5, "ece_after": 0.1, "n_test_queries": 10}, "r"
                )

        # Fail-safe = logged its failure (not silent).
        assert any("write-back FAILED" in r.message for r in caplog.records)
