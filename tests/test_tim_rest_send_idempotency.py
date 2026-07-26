from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_tim_rest_contract_protocol"
protocol_package = types.ModuleType(PACKAGE)
protocol_package.__path__ = [str(ROOT / "bbw_protocol")]
adapters_package = types.ModuleType(f"{PACKAGE}.adapters")
adapters_package.__path__ = [str(ROOT / "bbw_protocol" / "adapters")]
sign_module = types.ModuleType(f"{PACKAGE}.sign")
sign_module.TXIM_SDKAPPID = 0
sign_module.TXIM_SECRETKEY = ""
sign_module.gen_user_sig = lambda *_args, **_kwargs: "contract-usersig"
sys.modules[PACKAGE] = protocol_package
sys.modules[f"{PACKAGE}.adapters"] = adapters_package
sys.modules[f"{PACKAGE}.sign"] = sign_module
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.adapters.tim_rest",
    ROOT / "bbw_protocol" / "adapters" / "tim_rest.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("unable to load TIM REST adapter")
tim_rest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tim_rest
spec.loader.exec_module(tim_rest)
RestResult = tim_rest.RestResult
TimRestClient = tim_rest.TimRestClient


class CapturingTimRestClient(TimRestClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], object]] = []

    def call(self, command, body, *, admin=None):
        self.calls.append((command, body, admin))
        return RestResult(ok=True, action=command, data=body)


class TimRestSendIdempotencyTests(unittest.TestCase):
    def test_same_idempotency_key_reuses_tim_deduplication_identity(self) -> None:
        client = CapturingTimRestClient()

        first = client.send_text(
            "42",
            "9",
            "本地消息",
            idempotency_key="canonical-message-123",
        )
        second = client.send_text(
            "42",
            "9",
            "本地消息",
            idempotency_key="canonical-message-123",
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        first_body = client.calls[0][1]
        second_body = client.calls[1][1]
        self.assertEqual(first_body["MsgSeq"], second_body["MsgSeq"])
        self.assertEqual(first_body["MsgRandom"], second_body["MsgRandom"])
        self.assertGreater(int(first_body["MsgSeq"]), 0)

    def test_legacy_send_without_idempotency_key_keeps_random_mode(self) -> None:
        client = CapturingTimRestClient()

        result = client.send_text("42", "9", "旧通道消息")

        self.assertTrue(result.ok)
        body = client.calls[0][1]
        self.assertIn("MsgRandom", body)
        self.assertNotIn("MsgSeq", body)


if __name__ == "__main__":
    unittest.main()
