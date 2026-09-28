import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe as P

class T(unittest.TestCase):
    def test_seed(self):
        self.assertEqual(P.judge({}, 200, {"code": 200}, {"total": 1})[0], "SEED")
    def test_ok(self):
        prev = {"seeded": True, "airdrop_status": 200, "airdrop_keys": ["code"], "points_keys": ["total"]}
        self.assertEqual(P.judge(prev, 200, {"code": 200}, {"total": 5})[0], "OK")
    def test_schema_change(self):
        prev = {"seeded": True, "airdrop_status": 200, "airdrop_keys": ["code"], "points_keys": ["total"]}
        g, lines = P.judge(prev, 200, {"code": 200, "rh_season": {"status": "active"}}, {"total": 5})
        self.assertEqual(g, "ALERT"); self.assertIn("rh_season", lines[0])
    def test_status_change(self):
        prev = {"seeded": True, "airdrop_status": 400, "airdrop_keys": ["code", "message"], "points_keys": ["total"]}
        g, _ = P.judge(prev, 200, {"code": 200, "message": ""}, {"total": 5})
        self.assertEqual(g, "ALERT")
    def test_allocation(self):
        prev = {"seeded": True, "airdrop_status": 200, "airdrop_keys": ["code"], "points_keys": ["total"]}
        g, lines = P.judge(prev, 200, {"code": 200, "airdrops": [{"amount": "1234.5", "claimable": True}]}, {"total": 5})
        self.assertEqual(g, "IMMINENT"); self.assertIn("1,234.5", lines[0])
    def test_allocation_only_once(self):
        prev = {"seeded": True, "alloc_seen": True, "airdrop_status": 200,
                "airdrop_keys": P.key_paths({"airdrops": [{"amount": 1}]}), "points_keys": ["total"]}
        self.assertEqual(P.judge(prev, 200, {"airdrops": [{"amount": 1}]}, {"total": 5})[0], "OK")
    def test_zero_alloc_not_imminent(self):
        prev = {"seeded": True, "airdrop_status": 200, "airdrop_keys": ["code"], "points_keys": ["total"]}
        self.assertNotEqual(P.judge(prev, 200, {"code": 200, "amount": 0}, {"total": 5})[0], "IMMINENT")
    def test_env_alias(self):
        os.environ["X_REAL"] = "v"; os.environ["LIGHTER_AUTH_TOKEN_ENV"] = "X_REAL"
        self.assertEqual(P.env("LIGHTER_AUTH_TOKEN"), "v")
        del os.environ["LIGHTER_AUTH_TOKEN_ENV"]

if __name__ == "__main__":
    unittest.main()
