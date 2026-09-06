"""Offline checks for non-biometric parts of the supplied five-stage main.py.

No model downloads, face encoding, identity comparison, live search, or blockchain
broadcast is performed. Parser/UI fixtures below are test-only, not demo evidence.
"""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rich.console import Console

import cv2
import numpy as np
from web3 import Web3

import main as app


class OfflineChecks(unittest.TestCase):
    def setUp(self):
        # Never let a test accidentally make a real HTTP/API/RPC request.
        guard = patch("requests.sessions.Session.request",
                      side_effect=AssertionError("Network access is forbidden in this offline suite"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_hash_and_canonical_record(self):
        self.assertEqual(app.sha256(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(app.canonical({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        self.assertNotEqual(app.sha256(app.canonical({"a": 1})),
                            app.sha256(app.canonical({"a": 2})))

    def test_url_domain_boundaries(self):
        self.assertTrue(app.is_social("https://old.reddit.com/r/test"))
        self.assertTrue(app.is_social("https://www.instagram.com/p/test/"))
        self.assertFalse(app.is_social("https://reddit.com.example.org/post"))
        self.assertFalse(app.valid_url("javascript:alert(1)"))
        self.assertFalse(app.valid_url("https://user:password@example.org/"))
        self.assertFalse(app.valid_url("https://example.org/\x1b[31m"))

    def test_empty_search_is_not_a_match(self):
        self.assertEqual(app.parse_candidates({}, False), [])
        self.assertEqual(app.parse_candidates({"exact_matches": []}, True), [])
        with self.assertRaises(app.PipelineError):
            app.parse_candidates({"exact_matches": {}}, False)

    def test_invalid_image(self):
        with self.assertRaisesRegex(app.PipelineError, "Cannot decode"):
            app.detect_faces(b"this is not an image")

    def test_no_face(self):
        ok, encoded = cv2.imencode(".png", np.zeros((200, 200, 3), dtype=np.uint8))
        self.assertTrue(ok)
        with self.assertRaisesRegex(app.PipelineError, "No face"):
            app.detect_faces(encoded.tobytes())

    def test_atomic_report_save(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested/report.json"
            app.save_report(path, {"status": "pending"})
            app.save_report(path, {"status": "verified"})
            self.assertEqual(json.loads(path.read_text())["status"], "verified")
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_real_offline_signing(self):
        # Fresh throwaway key, never printed, never funded, never broadcast.
        account = Web3().eth.account.create()
        tx = {"chainId": app.CHAIN_ID, "from": account.address,
              "to": account.address, "value": 0, "nonce": 0, "gas": 100000,
              "maxPriorityFeePerGas": 1000000000, "maxFeePerGas": 3000000000,
              "data": app.PREFIX + app.canonical({"schema": "offline-signing-test"})}
        signed = account.sign_transaction(tx)
        self.assertEqual(Web3.keccak(signed.raw_transaction), signed.hash)
        self.assertEqual(Web3().eth.account.recover_transaction(signed.raw_transaction),
                         account.address)

    def test_local_tamper_rejected_before_rpc(self):
        record = {"image_sha256": app.sha256(b"original")}
        report = {"record": copy.deepcopy(record),
                  "record_hash": app.sha256(app.canonical(record))}
        report["record"]["image_sha256"] = app.sha256(b"altered")
        # No provider: this must fail before any RPC is attempted.
        with self.assertRaisesRegex(app.PipelineError, "Local record hash mismatch"):
            app.verify_chain(Web3(), report)


    def test_social_only_is_default(self):
        args = app.build_parser().parse_args(["--image", "unused-test-image.png"])
        self.assertTrue(args.social_only)
        self.assertEqual(args.output, Path("output/report.json"))

    def test_web_override_and_legacy_social_flag(self):
        parser = app.build_parser()
        self.assertFalse(parser.parse_args(["--image", "unused.png", "--include-web"]).social_only)
        self.assertTrue(parser.parse_args(["--image", "unused.png", "--social-only"]).social_only)
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--image", "unused.png", "--include-web", "--social-only"])

    def test_readback_parser_does_not_require_image(self):
        args = app.build_parser().parse_args(["--verify-report", "saved.json"])
        self.assertIsNone(args.image)
        self.assertEqual(args.verify_report, Path("saved.json"))

    def test_filter_retains_only_allowed_candidate_links(self):
        # Synthetic parser fixture; not a successful real search or social post.
        items = {"exact_matches": [
            {"link": "https://www.youtube.com/watch?v=parser-fixture",
             "thumbnail": "https://cdn.example.org/parser-fixture.png"},
            {"link": "https://news.example.org/parser-fixture"},
            {"link": "https://youtube.com.example.org/parser-fixture"},
            {"link": "https://www.youtube.com/watch?v=parser-fixture"},
            {"link": "javascript:invalid"},
        ]}
        social = app.parse_candidates(items, True)
        self.assertEqual(len(social), 1)
        self.assertTrue(social[0]["is_social_domain"])
        self.assertIsNone(social[0]["confidence"])
        self.assertIsNone(social[0]["relevance_score"])
        # Social filtering affects page links, not CDN/thumbnail URLs.
        self.assertEqual(social[0]["thumbnail"], "https://cdn.example.org/parser-fixture.png")
        self.assertEqual(len(app.parse_candidates(items, False)), 3)

    def test_empty_summary_has_five_unconfirmed_rows(self):
        stream = io.StringIO()
        with patch.object(app, "CONSOLE", Console(file=stream, width=150, force_terminal=False)):
            app.show_summary({}, Path("report.json"))
        output = stream.getvalue()
        self.assertIn("Run incomplete", output)
        self.assertEqual(output.count("Not confirmed"), 5)
        self.assertNotIn("✓", output)
        self.assertNotIn("\x1b", output)

    def test_hash_alone_is_not_transaction_confirmation(self):
        stream = io.StringIO()
        with patch.object(app, "CONSOLE", Console(file=stream, width=150, force_terminal=False)):
            app.show_summary({"tx_hash": "unbroadcast-test-reference"}, Path("report.json"))
        self.assertEqual(stream.getvalue().count("Not confirmed"), 5)

    def test_saved_receipt_does_not_imply_verified_record(self):
        stream = io.StringIO()
        # UI state only, not a fabricated RPC response.
        state = {"transaction_confirmation": {"status": 1}, "verification": {"verified": False}}
        with patch.object(app, "CONSOLE", Console(file=stream, width=150, force_terminal=False)):
            app.show_summary(state, Path("report.json"))
        rendered = stream.getvalue()
        self.assertIn("Run incomplete", rendered)
        self.assertEqual(rendered.count("Not confirmed"), 4)
        self.assertEqual(rendered.count("✓"), 1)

    def test_readback_summary_labels_saved_history(self):
        stream = io.StringIO()
        with patch.object(app, "CONSOLE", Console(file=stream, width=160, force_terminal=False)):
            app.show_summary({}, Path("report.json"), readback_only=True)
        rendered = stream.getvalue()
        for label in ("Face detected", "Candidate reviewed", "Face match confirmed"):
            self.assertIn(label + " (saved run)", rendered)

    def test_five_stage_headers_preserve_original_log_text(self):
        stream = io.StringIO()
        original = app._last_stage
        self.addCleanup(setattr, app, "_last_stage", original)
        app._last_stage = None
        with patch.object(app, "CONSOLE", Console(file=stream, width=160, force_terminal=False)):
            app.log(1, "Detecting face in input image...")
            app.log(1, "OK: display-only test")
            app.log(5, "Fetching transaction and receipt from Ethereum Sepolia...")
        rendered = stream.getvalue()
        self.assertEqual(app.STAGE_TOTAL, 5)
        self.assertEqual(rendered.count("Stage 1/5"), 1)
        self.assertIn("[1/5] Detecting face in input image...", rendered)
        self.assertIn("Stage 5/5", rendered)
        self.assertIn("[5/5] Fetching transaction and receipt from Ethereum Sepolia...", rendered)

    def test_network_wrapper_cleans_up_without_swallowing_error(self):
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True)
        called = []
        def operation():
            called.append(True)
            raise app.PipelineError("offline wrapper exception")
        with patch.object(app, "CONSOLE", console):
            with self.assertRaisesRegex(app.PipelineError, "offline wrapper exception"):
                app.network_call("Wrapper test", operation)
        self.assertEqual(called, [True])
        self.assertFalse(console._live_stack)

    def test_nonterminal_wrapper_returns_result_once_without_spinner(self):
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False)
        with patch.object(app, "CONSOLE", console), patch.object(console, "status") as status:
            self.assertEqual(app.network_call("Wrapper test", lambda value: value + 1, 2), 3)
            status.assert_not_called()
        self.assertEqual(stream.getvalue(), "")

    def test_record_hash_does_not_cover_outer_report_fields(self):
        # Characterizes the integrity boundary; does not exercise biometric code.
        record = {"schema": "image-source-proof/v1", "image_sha256": app.sha256(b"test-bytes")}
        report = {"record": record, "record_hash": app.sha256(app.canonical(record)),
                  "face_match_verification": {"attempted": False}}
        previous_hash = report["record_hash"]
        report["face_match_verification"]["reason"] = "local-only test annotation"
        self.assertEqual(app.sha256(app.canonical(report["record"])), previous_hash)
        report["record"]["image_sha256"] = app.sha256(b"different-bytes")
        self.assertNotEqual(app.sha256(app.canonical(report["record"])), previous_hash)

    def test_summary_does_not_interpret_external_markup(self):
        stream = io.StringIO()
        url = "https://example.org/[bold]parser-fixture[/bold]"
        with patch.object(app, "CONSOLE", Console(file=stream, width=160, force_terminal=False)):
            app.show_summary({"record": {"matched_url": url}}, Path("report.json"))
        self.assertIn(url, stream.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
