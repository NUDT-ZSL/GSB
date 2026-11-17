import unittest

from workbench.errors import E_STATE, BizError
from workbench.statemachine import TCPStateMachine


class TestStateMachine(unittest.TestCase):
    def test_legal_full_path(self):
        sm = TCPStateMachine()
        self.assertEqual(sm.state, "CLOSED")
        sm.transition("SYN")
        self.assertEqual(sm.state, "SYN_SENT")
        sm.transition("SYN_ACK")
        self.assertEqual(sm.state, "ESTABLISHED")
        sm.transition("FIN")
        self.assertEqual(sm.state, "FIN_WAIT")
        sm.transition("RCV_FIN")
        self.assertEqual(sm.state, "CLOSE_WAIT")
        sm.transition("FIN_ACK")
        self.assertEqual(sm.state, "CLOSED")

    def test_peer_initiated_close(self):
        sm = TCPStateMachine()
        sm.transition("SYN")
        sm.transition("SYN_ACK")
        sm.transition("RCV_FIN")
        self.assertEqual(sm.state, "CLOSE_WAIT")

    def test_illegal_transition_rejected_with_e_state(self):
        sm = TCPStateMachine()
        with self.assertRaises(BizError) as ctx:
            sm.transition("SYN_ACK")
        self.assertEqual(ctx.exception.code, E_STATE)
        self.assertEqual(sm.state, "CLOSED")  # 保持原状态

    def test_illegal_transition_mid_path_keeps_state(self):
        sm = TCPStateMachine()
        sm.transition("SYN")
        with self.assertRaises(BizError) as ctx:
            sm.transition("FIN")
        self.assertEqual(ctx.exception.code, E_STATE)
        self.assertEqual(sm.state, "SYN_SENT")


if __name__ == "__main__":
    unittest.main()
