import unittest
from concurrent.futures import ThreadPoolExecutor
from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.contracts.common import CallContext

A = "a" * 32
B = "b" * 32

class ContextGateTests(unittest.TestCase):
    def test_initial_context_rejects_foreign_collection(self):
        gate = ActiveContextGate()
        self.assertTrue(gate.is_current(CallContext(None, 0)))
        self.assertFalse(gate.is_current(CallContext(A, 0)))

    def test_activation_invalidates_old_delivery(self):
        gate = ActiveContextGate()
        installed, actual = gate.try_activate(CallContext(None, 0), A)
        self.assertTrue(installed)
        self.assertEqual(actual, CallContext(A, 1))
        self.assertFalse(gate.is_current(CallContext(None, 0)))
        self.assertTrue(gate.is_current(actual))

    def test_stale_request_preserves_current_context(self):
        gate = ActiveContextGate()
        gate.try_activate(CallContext(None, 0), A)
        self.assertEqual(gate.try_activate(CallContext(None, 0), B), (False, CallContext(A, 1)))

    def test_return_to_previous_collection_does_not_revive_old_results(self):
        gate = ActiveContextGate()
        _, first = gate.try_activate(gate.current, A)
        _, second = gate.try_activate(first, B)
        _, third = gate.try_activate(second, A)
        self.assertEqual(third, CallContext(A, 3))
        self.assertFalse(gate.is_current(first))

    def test_malformed_ids_leave_state_unchanged(self):
        for identity in ["", "K1", "A" * 32, "g" * 32, None, 123]:
            with self.subTest(identity=identity):
                gate = ActiveContextGate()
                with self.assertRaises(ValueError):
                    gate.try_activate(CallContext(None, 0), identity)
                self.assertEqual(gate.current, CallContext(None, 0))

    def test_competing_activations_have_one_winner(self):
        gate = ActiveContextGate()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: gate.try_activate(CallContext(None, 0), A)[0], range(32)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(gate.current, CallContext(A, 1))

    def test_generation_overflow_does_not_mutate_state(self):
        gate = ActiveContextGate(max_generation=1)
        gate.try_activate(gate.current, A)
        with self.assertRaises(OverflowError):
            gate.try_activate(gate.current, B)
        self.assertEqual(gate.current, CallContext(A, 1))

    def test_boolean_generation_cannot_impersonate_integer_generation(self):
        gate = ActiveContextGate()
        gate.try_activate(gate.current, A)
        self.assertFalse(gate.is_current(CallContext(A, True)))
        with self.assertRaises(ValueError):
            gate.try_activate(CallContext(A, True), B)
        self.assertEqual(gate.current, CallContext(A, 1))
