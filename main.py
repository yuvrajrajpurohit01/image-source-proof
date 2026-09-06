"""Image-source provenance: detection, exact-image search, reviewed record, Sepolia.

Face encodings are used only to CONFIRM a manually reviewed candidate (1:1
verification of two photos already in hand) - never to search for, discover,
or identify candidates from a face. No profile enrichment or fabricated results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import TimeExhausted
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

CHAIN_ID = 11155111
EXPLORER = "https://sepolia.etherscan.io/tx/"
PREFIX = b"IMAGE_SOURCE_PROOF_V1\n"
SOCIAL = ("twitter.com", "x.com", "instagram.com", "facebook.com",
          "linkedin.com", "reddit.com", "youtu.be", "youtube.com")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CONSOLE = Console(highlight=False, markup=False)
ERROR_CONSOLE = Console(stderr=True, highlight=False, markup=False)
STAGE_TOTAL = 5
STAGE_TITLES = {1: "Local face detection", 2: "Image-source search and review",
                3: "Face-match confirmation (verification, not search)",
                4: "Sepolia record", 5: "On-chain read-back verification"}
_last_stage = None

# --- Face-match confirmation (stage 3): pinned OpenCV Zoo models -----------------
# Used only to compare two already-selected photos (1:1 verification), the same
# category of check as matching a selfie to an ID photo - never to search the
# web or a database for a face. Multi-MB binaries are fetched once, from public
# mirrors, and accepted only if their SHA-256 matches the pin below, so a stale
# git-lfs pointer or a tampered download is rejected rather than silently used.
MODELS_DIR = Path(__file__).resolve().with_name("models")
YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
YUNET_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
    "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/"
    "face_detection_yunet_2023mar.onnx",
)
SFACE_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
    "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "https://huggingface.co/opencv/face_recognition_sface/resolve/main/"
    "face_recognition_sface_2021dec.onnx",
)
# Thresholds published in OpenCV's SFace demo/docs: cosine >= this, or
# L2 <= this, indicates the same person. Treat both as heuristics, not proof.
COSINE_MATCH_THRESHOLD = 0.363
L2_MATCH_THRESHOLD = 1.128
_verifier_cache: tuple | None = None


class PipelineError(Exception):
    """An actionable error that should not print a traceback or credentials."""


def log(stage: int, message: str) -> None:
    """Keep original log text, add stage dividers and truthful status colors."""
    global _last_stage
    if stage != _last_stage:
        CONSOLE.rule(Text(f"Stage {stage}/{STAGE_TOTAL} | {STAGE_TITLES[stage]}", style="bold cyan"))
        _last_stage = stage
    style = "green" if message.startswith("OK:") else "cyan"
    CONSOLE.print(Text(f"[{stage}/{STAGE_TOTAL}] {message}", style=style))


def network_call(label: str, operation, *args, **kwargs):
    """Animate only around a real synchronous network call or receipt polling.

    No percentage or delay is invented. Rich stops the spinner on success,
    exception, or cancellation. Redirected logs get no animation/control codes.
    """
    if not CONSOLE.is_terminal:
        return operation(*args, **kwargs)
    with CONSOLE.status(Text(label, style="cyan"), spinner="dots"):
        return operation(*args, **kwargs)


def show_summary(report: dict, output: Path, *, readback_only: bool = False) -> None:
    """Derive every badge from observed state; never mark pending work successful."""
    verified = report.get("verification", {}).get("verified") is True
    mined = verified or report.get("transaction_confirmation", {}).get("status") == 1
    face_match = report.get("face_match_verification") or {}
    statuses = [
        ("Face detected", report.get("face_detection", {}).get("count", 0) > 0),
        ("Candidate reviewed", bool(report.get("selected_source"))),
        ("Face match confirmed", face_match.get("same_face_verdict") == "same_face"),
        ("Blockchain tx confirmed", mined),
        ("Verification passed", verified),
    ]
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, success in statuses:
        # Checks refer to local report history for the first three rows on read-back.
        history = " (saved run)" if readback_only and label in (
            "Face detected", "Candidate reviewed", "Face match confirmed") else ""
        badge = "✓" if success else "—"
        grid.add_row(Text(label + history), Text(
            f"{badge} {'Yes' if success else 'Not confirmed'}",
            style="green" if success else "yellow"))
    values = [("Matched URL", report.get("record", {}).get("matched_url")),
              ("Transaction hash", report.get("tx_hash")),
              ("Explorer", report.get("explorer_link")),
              ("Report", str(output.resolve()))]
    for label, value in values:
        grid.add_row(Text(label), Text(str(value or "Not available"), overflow="fold"))
    CONSOLE.print(Panel(grid, title="Verified record" if verified else "Run incomplete",
                        border_style="green" if verified else "yellow"))
    CONSOLE.print(Text("Verification covers record integrity and inclusion, not identity or source truth.",
                       style="dim"))


def clean_error(value: object) -> str:
    """Preserve provider errors but remove credentials and terminal controls."""
    text = str(value)
    for name in ("SERPAPI_API_KEY", "IMGBB_API_KEY", "WALLET_PRIVATE_KEY",
                 "SEPOLIA_RPC_URL"):
        secret = os.getenv(name, "").strip()
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL redacted]", text)
    return "".join(c for c in text if c.isprintable())[:1500]


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PipelineError(f"Missing {name}. Set it in .env beside main.py.")
    return value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict) -> bytes:
    """Stable UTF-8 serialization shared by writing and verification."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def save_report(path: Path, report: dict) -> None:
    """Atomically checkpoint before broadcast, after broadcast, and after read-back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                    allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def detect_faces(raw: bytes) -> dict:
    """Detect rectangles only; never encode, crop for search, or identify people."""
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise PipelineError("Cannot decode image. Use a valid JPEG or PNG.")
    height, width = decoded.shape[:2]
    # Bound detector workload on large laptop-camera images.
    scale = min(1.0, 1600 / max(height, width))
    small = cv2.resize(decoded, (round(width * scale), round(height * scale)))
    gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    classifier = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if classifier.empty():
        raise PipelineError("OpenCV cascade is missing. Reinstall requirements.txt.")
    boxes = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                        minSize=(30, 30))
    boxes = sorted(([int(round(v / scale)) for v in box] for box in boxes),
                   key=lambda box: box[2] * box[3], reverse=True)
    if not boxes:
        raise PipelineError("No face detected. Retry with a clear, upright, front-facing photo.")
    # Haar detection does not produce a calibrated probability. Do not invent one.
    return {"method": "OpenCV Haar frontal-face cascade", "confidence": None,
            "count": len(boxes), "boxes_xywh": boxes,
            "largest_box_xywh": boxes[0], "image_dimensions": [width, height]}


def api_json(method: str, url: str, **kwargs) -> dict:
    """Bound network waits and show useful HTTP/provider errors without secrets."""
    try:
        response = network_call(f"Waiting for {urlparse(url).hostname}...",
                                requests.request, method, url, timeout=(15, 120), **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise PipelineError(f"Provider returned non-JSON (HTTP {response.status_code}).")
        if not isinstance(data, dict):
            raise PipelineError("Unexpected API response: expected a JSON object.")
        if not response.ok or data.get("error"):
            raise PipelineError(
                f"HTTP {response.status_code}: {clean_error(data.get('error', data))}. "
                "Check API key, quota, and provider status.")
        return data
    except requests.RequestException as exc:
        raise PipelineError(f"API connection failed: {clean_error(exc)}. Check network and retry.") from exc


def upload_image(raw: bytes) -> str:
    """Upload the unchanged full input; request host deletion after one hour."""
    data = api_json("POST", "https://api.imgbb.com/1/upload",
                    data={"key": required("IMGBB_API_KEY"), "expiration": "3600"},
                    files={"image": ("input-image", raw)})
    url = data.get("data", {}).get("url")
    if data.get("success") is not True or not valid_url(url):
        raise PipelineError("ImgBB did not return a valid image URL.")
    return url


def valid_url(url: object) -> bool:
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme in ("http", "https") and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


def is_social(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL)


def parse_candidates(data: dict, social_only: bool) -> list[dict]:
    """Only use API exact-image results; URL domains do not establish identity."""
    candidates, seen = [], set()
    items = data.get("exact_matches", [])
    if not isinstance(items, list):
        raise PipelineError("SerpAPI exact_matches schema changed; inspect API documentation.")
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("link")
        if not valid_url(url) or url in seen:
            continue
        social = is_social(url)
        if social_only and not social:
            continue
        seen.add(url)
        candidates.append({"url": url, "title": item.get("title"),
                           "source": item.get("source"), "is_social_domain": social,
                           "api_position": item.get("position"),
                           "confidence": item.get("confidence"),
                           "relevance_score": item.get("relevance_score"),
                           "thumbnail": item.get("thumbnail"),
                           "search_type": "exact_matches",
                           "assessment": "unreviewed image-source candidate"})
    return candidates


def reverse_search(image_url: str, social_only: bool) -> tuple[list[dict], str | None]:
    """A fresh paid/quota-counted API call; no local fixtures or cached fake matches."""
    data = api_json("GET", "https://serpapi.com/search.json", params={
        "engine": "google_lens", "type": "exact_matches", "url": image_url,
        "api_key": required("SERPAPI_API_KEY"), "no_cache": "true"})
    candidates = parse_candidates(data, social_only)
    if not candidates:
        raise PipelineError("No eligible exact-image source candidates found. Nothing was "
                            "written on-chain. Retry with an already publicly indexed photo "
                            "you own or have permission to use. New uploads may not be indexed.")
    return candidates, data.get("search_metadata", {}).get("id")


def review_candidate(candidates: list[dict]) -> dict:
    """Require human image-source review, never auto-declare a person match."""
    for i, item in enumerate(candidates, 1):
        label = "social domain" if item["is_social_domain"] else "web source"
        CONSOLE.print(Text(f"  {i}. [{label}] {item['url']}"))
    CONSOLE.print("Open a URL and check that the page contains the same whole photograph.")
    CONSOLE.print("Domain membership alone does not prove this is a post or a correct match.")
    while True:
        choice = CONSOLE.input("Number of the source you reviewed (q = exit): ").strip()
        if choice.lower() == "q":
            raise PipelineError("Cancelled; no transaction sent.")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            selected = dict(candidates[int(choice) - 1])
            selected["assessment"] = "user-reviewed image source; not identity verification"
            return selected
        CONSOLE.print("Enter one of the displayed numbers, or q.", style="yellow")


def ensure_model(urls: tuple[str, ...], path: Path, expected_sha256: str) -> Path:
    """Fetch a pinned OpenCV Zoo model once; accept it only if its hash matches.

    These verification models are multi-MB binaries not bundled with this repo.
    Trying several public mirrors and checking a pinned SHA-256 means a stale
    git-lfs pointer, an HTML error page, or a tampered file is rejected instead
    of silently loaded as if it were the real model.
    """
    if path.exists() and sha256(path.read_bytes()) == expected_sha256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error = "no mirror was reachable"
    for url in urls:
        try:
            response = network_call(f"Downloading {path.name} from {urlparse(url).hostname}...",
                                    requests.get, url, timeout=(15, 180))
        except requests.RequestException as exc:
            last_error = clean_error(exc)
            continue
        if not response.ok:
            last_error = f"HTTP {response.status_code} from {urlparse(url).hostname}"
            continue
        data = response.content
        if sha256(data) != expected_sha256:
            last_error = (f"downloaded bytes from {urlparse(url).hostname} did not match the "
                          "pinned hash (likely an LFS pointer or redirect page, not the model)")
            continue
        path.write_bytes(data)
        return path
    raise PipelineError(
        f"Could not fetch a verified copy of {path.name} ({last_error}). Download it manually "
        f"from https://github.com/opencv/opencv_zoo/tree/main/models and save it at {path}.")


def load_face_verifier() -> tuple:
    """Lazily load YuNet (detector) + SFace (encoder), only when stage 3 runs."""
    global _verifier_cache
    if _verifier_cache is None:
        detector_path = ensure_model(YUNET_URLS, YUNET_PATH, YUNET_SHA256)
        recognizer_path = ensure_model(SFACE_URLS, SFACE_PATH, SFACE_SHA256)
        detector = cv2.FaceDetectorYN.create(str(detector_path), "", (320, 320),
                                             score_threshold=0.7)
        recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        _verifier_cache = (detector, recognizer)
    return _verifier_cache


def detect_and_align(detector, recognizer, raw: bytes):
    """Return one aligned 112x112 face crop for encoding, or None if none found.

    Only the largest detected face is used - this compares two specific photos
    already in hand, never a database of many faces.
    """
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    if height == 0 or width == 0:
        return None
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    if faces is None or len(faces) == 0:
        return None
    largest = max(faces, key=lambda face: face[2] * face[3])
    return recognizer.alignCrop(image, largest)


def fetch_candidate_image(candidate: dict) -> bytes | None:
    """Best-effort real image fetch for comparison; never guesses or fabricates one.

    Tries the API's own thumbnail of the matched image first - it is already the
    specific image the search matched - then falls back to the reviewed page URL
    only if that URL happens to serve an image directly rather than a webpage.
    """
    for url in (candidate.get("thumbnail"), candidate.get("url")):
        if not valid_url(url):
            continue
        try:
            response = network_call(f"Fetching candidate image ({urlparse(url).hostname})...",
                                    requests.get, url, timeout=(15, 60),
                                    headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException:
            continue
        content_type = response.headers.get("Content-Type", "")
        if not response.ok or not content_type.startswith("image/"):
            continue
        if len(response.content) > MAX_IMAGE_BYTES:
            continue
        return response.content
    return None


def verify_face_match(input_raw: bytes, candidate: dict) -> dict:
    """Confirm whether an ALREADY-FOUND candidate shows the same face as the input.

    This never searches for or discovers candidates from a face encoding - the
    stage 2 whole-image search already located the candidate, and a human
    already reviewed and selected it. Encodings here only compare two specific,
    already-selected photos (1:1 verification) - the same category of check
    used to confirm a selfie against an ID photo - not a 1:many identity search.
    Raw encoding vectors are never stored; only a hash of each is kept.
    """
    result = {"attempted": True, "same_face_verdict": "unable_to_verify", "reason": None,
              "distance_metric": "cosine_and_l2", "cosine_similarity": None,
              "l2_distance": None, "cosine_threshold": COSINE_MATCH_THRESHOLD,
              "l2_threshold": L2_MATCH_THRESHOLD, "input_face_encoding_sha256": None,
              "candidate_face_encoding_sha256": None, "candidate_image_source": None}
    try:
        detector, recognizer = load_face_verifier()
    except PipelineError as exc:
        result["reason"] = str(exc)
        return result
    candidate_bytes = fetch_candidate_image(candidate)
    if candidate_bytes is None:
        result["reason"] = "Could not fetch a direct image from the reviewed source to compare."
        return result
    result["candidate_image_source"] = "thumbnail" if candidate.get("thumbnail") else "page_url"
    input_face = detect_and_align(detector, recognizer, input_raw)
    if input_face is None:
        result["reason"] = "Verifier could not detect a face in the input image."
        return result
    candidate_face = detect_and_align(detector, recognizer, candidate_bytes)
    if candidate_face is None:
        result["reason"] = "No face detected in the candidate image; unable to verify."
        return result
    input_feature = recognizer.feature(input_face)
    candidate_feature = recognizer.feature(candidate_face)
    result["input_face_encoding_sha256"] = sha256(input_feature.tobytes())
    result["candidate_face_encoding_sha256"] = sha256(candidate_feature.tobytes())
    cosine = float(recognizer.match(input_feature, candidate_feature,
                                    cv2.FaceRecognizerSF_FR_COSINE))
    l2 = float(recognizer.match(input_feature, candidate_feature,
                                cv2.FaceRecognizerSF_FR_NORM_L2))
    result["cosine_similarity"] = cosine
    result["l2_distance"] = l2
    result["same_face_verdict"] = ("same_face" if cosine >= COSINE_MATCH_THRESHOLD
                                    and l2 <= L2_MATCH_THRESHOLD else "different_or_uncertain")
    return result


def connect_chain() -> Web3:
    """Reject every network except Sepolia to prevent accidental mainnet spending."""
    w3 = Web3(Web3.HTTPProvider(required("SEPOLIA_RPC_URL"),
                              request_kwargs={"timeout": 30}))
    if not network_call("Checking Sepolia RPC connection...", w3.is_connected):
        raise PipelineError("Cannot connect to Sepolia. Check SEPOLIA_RPC_URL and network.")
    if network_call("Reading chain ID...", lambda: w3.eth.chain_id) != CHAIN_ID:
        raise PipelineError("RPC is not Ethereum Sepolia (11155111). Use a Sepolia RPC URL.")
    return w3


def write_chain(w3: Web3, record: dict, report: dict, output: Path) -> str:
    """Sign a zero-value self transaction carrying the complete canonical record."""
    account = w3.eth.account.from_key(required("WALLET_PRIVATE_KEY"))
    if network_call("Checking test wallet code...", w3.eth.get_code, account.address):
        raise PipelineError("Use a fresh ordinary test wallet without contract/delegation code.")
    payload = PREFIX + canonical(record)
    if len(payload) > 16000:
        raise PipelineError("Record exceeds the 16 KB demo limit. Choose a shorter source URL.")
    latest = network_call("Reading latest block...", w3.eth.get_block, "latest")
    base = latest.get("baseFeePerGas")
    if base is None:
        raise PipelineError("RPC did not return Sepolia EIP-1559 base fee.")
    tip = network_call("Reading suggested priority fee...", lambda: w3.eth.max_priority_fee)
    tx = {"chainId": CHAIN_ID, "from": account.address, "to": account.address,
          "value": 0, "nonce": network_call("Reading pending nonce...",
              w3.eth.get_transaction_count, account.address, "pending"),
          "data": payload, "maxPriorityFeePerGas": tip,
          "maxFeePerGas": 2 * base + tip}
    tx["gas"] = (network_call("Estimating transaction gas...", w3.eth.estimate_gas, tx) * 120 + 99) // 100
    upper_bound = tx["gas"] * tx["maxFeePerGas"]
    if network_call("Reading test ETH balance...", w3.eth.get_balance,
                    account.address, "pending") < upper_bound:
        raise PipelineError(f"Insufficient Sepolia ETH for wallet {account.address}. "
                            "Get test funds: https://www.alchemy.com/faucets/ethereum-sepolia")
    log(4, f"Maximum transaction fee: {w3.from_wei(upper_bound, 'ether')} Sepolia ETH")
    signed = account.sign_transaction(tx)
    # Save the deterministically known hash BEFORE broadcasting; a timeout may occur
    # after the node accepts a transaction. Never automatically send a replacement.
    tx_hash = Web3.to_hex(signed.hash)
    report.update({"status": "broadcast_prepared", "tx_hash": tx_hash,
                   "explorer_link": EXPLORER + tx_hash, "sender": account.address})
    save_report(output, report)
    returned_hash = Web3.to_hex(network_call("Broadcasting signed transaction...",
        w3.eth.send_raw_transaction, signed.raw_transaction))
    if returned_hash.lower() != tx_hash.lower():
        raise PipelineError("RPC returned an unexpected transaction hash; inspect report before retry.")
    report["status"] = "pending"
    save_report(output, report)
    log(4, f"Transaction submitted: {tx_hash}\n       View: {EXPLORER}{tx_hash}")
    try:
        receipt = network_call("Waiting for transaction confirmation on Sepolia...",
            w3.eth.wait_for_transaction_receipt, tx_hash, timeout=180, poll_latency=2)
    except TimeExhausted as exc:
        raise PipelineError("Transaction still pending. Use --verify-report to resume; "
                            "do not rerun image search or broadcast a duplicate.") from exc
    if receipt.status != 1:
        raise PipelineError("Transaction reverted (receipt status 0). Inspect explorer and gas.")
    report["transaction_confirmation"] = {"status": 1, "block_number": receipt.blockNumber}
    save_report(output, report)
    log(4, "OK: transaction included in a block.")
    return tx_hash


def verify_chain(w3: Web3, report: dict) -> dict:
    """Fetch actual transaction bytes and receipt, then compare with local record."""
    record = report["record"]
    if report.get("record_hash") != sha256(canonical(record)):
        raise PipelineError("Local record hash mismatch: report was modified or corrupted.")
    tx = network_call("Fetching transaction input...", w3.eth.get_transaction, report["tx_hash"])
    receipt = network_call("Fetching transaction receipt...",
                           w3.eth.get_transaction_receipt, report["tx_hash"])
    if receipt.status != 1 or tx.get("blockNumber") is None:
        raise PipelineError("Transaction is not successfully mined yet.")
    if int(tx.get("chainId", 0)) != CHAIN_ID or int(tx["value"]) != 0:
        raise PipelineError("Unexpected transaction chain or value.")
    if tx["from"].lower() != report["sender"].lower() or not tx.get("to") \
            or tx["to"].lower() != tx["from"].lower():
        raise PipelineError("Transaction sender/recipient does not match the saved self transaction.")
    actual = bytes(tx["input"])
    if actual != PREFIX + canonical(record):
        raise PipelineError("On-chain bytes differ from the expected record.")
    # Ensure receipt and transaction agree with the node's current canonical block.
    block = network_call("Checking canonical block...", w3.eth.get_block, receipt.blockNumber)
    if block.hash != receipt.blockHash or tx["blockHash"] != receipt.blockHash:
        raise PipelineError("Block changed during verification; retry --verify-report.")
    decoded = json.loads(actual[len(PREFIX):].decode("utf-8"))
    CONSOLE.print(Text(json.dumps(decoded, indent=2, ensure_ascii=False)))
    return {"verified": True, "block_number": receipt.blockNumber,
            "block_hash": Web3.to_hex(receipt.blockHash),
            "block_timestamp": datetime.fromtimestamp(block.timestamp, timezone.utc).isoformat(),
            "confirmations": network_call("Reading confirmation count...",
                                          lambda: w3.eth.block_number) - receipt.blockNumber + 1,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "scope": "record integrity and inclusion; not identity, source truth, or finality"}


def build_parser() -> argparse.ArgumentParser:
    """Default to social domains; retain --social-only for older commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", type=Path, help="Consented whole JPEG/PNG to reverse-search")
    mode.add_argument("--verify-report", type=Path, help="Read back an existing transaction; no write")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--include-web", dest="social_only", action="store_false",
                       help="Explicitly include non-social web sources")
    scope.add_argument("--social-only", dest="social_only", action="store_true",
                       help="Social-domain candidates only (default; compatible with old commands)")
    parser.set_defaults(social_only=True)
    parser.add_argument("--output", type=Path, default=Path("output/report.json"))
    return parser


def main() -> int:
    global _last_stage
    _last_stage = None
    args = build_parser().parse_args()
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    report = {}
    output = args.verify_report or args.output
    try:
        if args.verify_report:
            report = json.loads(output.read_text(encoding="utf-8"))
            log(5, "Fetching transaction and receipt from Ethereum Sepolia...")
            report["verification"] = verify_chain(connect_chain(), report)
            report["status"] = "verified"
            report.pop("error", None)
            save_report(output, report)
            log(5, f"OK: record matches. Report: {output.resolve()}")
            show_summary(report, output, readback_only=True)
            return 0

        if output.exists():
            raise PipelineError("Output already exists. Use --verify-report to resume, or "
                                "--output output/another-run.json for a new run.")
        report = {"status": "started", "image_path": str(args.image.resolve()),
                  "face_detection_confidence": None, "matched_urls": [],
                  "face_match_verification": None,
                  "record_hash": None, "tx_hash": None, "explorer_link": None,
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "search_scope": "social_only" if args.social_only else "include_web"}
        save_report(output, report)
        log(1, "Detecting face in input image...")
        if args.image.stat().st_size > MAX_IMAGE_BYTES:
            raise PipelineError("Image exceeds the 20 MiB local limit. Use a smaller JPEG/PNG.")
        raw = args.image.read_bytes()
        report["face_detection"] = detect_faces(raw)
        log(1, f"OK: {report['face_detection']['count']} face(s) detected. "
               "Confidence unavailable; no identity embedding generated.")
        # Check setup before uploading a photo or consuming a search credit.
        required("SERPAPI_API_KEY")
        required("IMGBB_API_KEY")
        Web3().eth.account.from_key(required("WALLET_PRIVATE_KEY"))
        w3 = connect_chain()
        log(2, "Uploading whole photo to ImgBB (requested expiry: 1 hour)...")
        image_url = upload_image(raw)
        log(2, "Running genuine whole-image Exact Matches search via SerpAPI...")
        candidates, search_id = reverse_search(image_url, args.social_only)
        report.update({"candidates": candidates, "search_id": search_id,
                       "status": "awaiting_source_review"})
        save_report(output, report)
        selected = review_candidate(candidates)
        report["matched_urls"] = [selected["url"]]
        report["selected_source"] = selected
        log(2, f"OK: user-reviewed image source: {selected['url']}")
        log(3, "Comparing detected faces to confirm the reviewed candidate "
               "(confirmation of an already-found match, not a search step)...")
        face_match = verify_face_match(raw, selected)
        report["face_match_verification"] = face_match
        save_report(output, report)
        if face_match["same_face_verdict"] == "same_face":
            log(3, f"OK: same-face match (cosine={face_match['cosine_similarity']:.3f} "
                   f">= {COSINE_MATCH_THRESHOLD}, l2={face_match['l2_distance']:.3f} "
                   f"<= {L2_MATCH_THRESHOLD}).")
        elif face_match["same_face_verdict"] == "different_or_uncertain":
            log(3, f"NOTE: faces do not clearly match (cosine={face_match['cosine_similarity']:.3f}, "
                   f"l2={face_match['l2_distance']:.3f}). Recorded as-is; the run continues since "
                   "the source was already human-reviewed in stage 2.")
        else:
            log(3, f"NOTE: unable to verify - {face_match['reason']}")
        record = {"schema": "image-source-proof/v1", "image_sha256": sha256(raw),
                  "matched_url": selected["url"],
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "search_type": "whole_image_exact_matches",
                  "assertion": "user-reviewed image source; not identity verification"}
        report.update({"record": record, "record_hash": sha256(canonical(record)),
                       "timestamp": record["timestamp"], "chain_id": CHAIN_ID})
        log(4, "Building tamper-evident record and uploading to Ethereum Sepolia...")
        save_report(output, report)
        write_chain(w3, record, report, output)
        log(5, "Re-fetching transaction and comparing its actual input bytes...")
        report["verification"] = verify_chain(w3, report)
        report["status"] = "verified"
        save_report(output, report)
        log(5, f"OK: record matches. Verification complete. Report: {output.resolve()}")
        show_summary(report, output)
        return 0
    except (KeyboardInterrupt, EOFError):
        message = "Cancelled. If a transaction hash was saved, verify that report before rerunning."
    except Exception as exc:
        message = f"{type(exc).__name__}: {clean_error(exc)}"
        if report.get("tx_hash"):
            message += " Transaction hash is saved; use --verify-report before any new write."
        elif not isinstance(exc, PipelineError):
            message += " Check the input, wallet key, RPC access, test funds, and API settings."
    ERROR_CONSOLE.print(Text(f"ERROR: {message}", style="bold red"))
    if report:
        report.update({"status": "incomplete", "error": message,
                       "verification": {"verified": False}})
        try:
            save_report(output, report)
        except OSError as exc:
            ERROR_CONSOLE.print(Text(f"Cannot save report: {clean_error(exc)}", style="bold red"))
        show_summary(report, output, readback_only=bool(args.verify_report))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())