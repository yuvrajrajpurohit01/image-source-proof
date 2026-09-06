# Validation report

**Result: 21 offline tests passed against the uploaded five-stage `main.py`.**

This report covers non-biometric behavior: initial face-presence error handling,
source-URL parsing, CLI defaults, terminal presentation, local report handling,
canonical hashing and offline transaction signing. It does **not** validate the
added facial-identification stage or establish a successful public end-to-end run.

## Evidence and reproducibility

| Item | Observed value |
|---|---|
| Review date | September 6, 2026 |
| Python | 3.12.13 |
| Platform | Linux x86-64 |
| Tests | 21 passed; 0 failed |
| Test isolation | Temporary directory containing copies of the uploaded application and updated tests |
| Live HTTP/API/RPC access | Blocked in each test through the Requests session interface |
| Model downloads or face comparisons | None |
| Blockchain broadcasts | None |

Application SHA-256, for the uploaded `main(1).py` tested as `main.py`:

```text
82b1d2d070d3bdb506547d72303039a28b386af3c909fad9cfa7284f128da61b
```

Updated `test_pipeline.py` SHA-256:

```text
bef417a875a1aab7f2c71095b3f13bfe4eb039160f4aebe6c95bf4ea27b9550a
```

These digests identify the files used for this run. If either file changes,
rerun the tests and update this report rather than treating these results as
validation of the new revision.

### Installed direct dependencies in the test environment

| Package | Observed version |
|---|---|
| `numpy` | `1.26.4` |
| `opencv-python-headless` | `4.10.0.84` |
| `requests` | `2.32.5` |
| `python-dotenv` | `1.1.1` |
| `web3` | `7.13.0` |
| `rich` | `14.1.0` |

An updated `requirements.txt` was not attached for this review. These are the
installed versions used for this test run, not certification of the dependency
file or the user's Windows environment. Python 3.11.9 on Windows/macOS was not
tested during this update.

## What the 21 tests cover

| Area | Tests | Checks |
|---|---:|---|
| Hashing and reports | 3 | SHA-256 known answer and canonical JSON; atomic report saves; record-hash boundary excluding outer report fields |
| Source URL handling | 3 | Domain boundaries and invalid URLs; empty/malformed result lists; social filtering, deduplication, thumbnail retention and missing scores |
| Initial detection errors | 2 | Invalid image bytes and actual Haar no-face detection on a generated blank image |
| Offline signing and tampering | 2 | Real EIP-1559 signing/hash/signer recovery with an unfunded throwaway key; modified record rejected before RPC |
| CLI behavior | 3 | Social-only default; explicit web override and incompatible flags; read-back mode without an image argument |
| Terminal presentation | 6 | Five incomplete summary rows; hash alone unconfirmed; receipt distinct from verification; saved-run labels; five-stage headings; literal URL rendering |
| Network-status wrapper | 2 | Exception propagation and spinner cleanup; non-terminal result return without animation |
| **Total** | **21** | **All passed** |

The blank image contains no person. Parser fixtures use test-only source strings;
UI fixtures describe isolated presentation states. They are not real discovered
posts, model outcomes or receipts from a blockchain node. No such fixture is added
to the production application.

The HTTP guard is a regression-test safeguard, not a general network sandbox.
The suite does not exercise model retrieval or any alternative transport.

## Run these offline checks locally

Place the updated `test_pipeline.py` beside the same five-stage `main.py` and use
the project's existing environment with its dependencies installed.

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_pipeline.py
```

Activated environment, macOS or Linux:

```bash
python -m unittest -v test_pipeline.py
```

The suite imports the application but does not invoke its full pipeline. It needs
no API keys, wallet funds, photo of a person, model files or `.env` configuration.
A fresh signing key is generated only for the offline signing test, is not printed,
and is never funded or broadcast.

For the tested revision, the final unittest summary reports 21 tests and `OK`.
A local failure should be investigated as a code/environment mismatch or regression;
do not edit the expected count or suppress a failing assertion merely to get a pass.

## Important integrity and presentation findings

1. **A saved transaction hash is not confirmation.** A successful receipt or
   completed record read-back supplies the confirmation state used by the panel.
2. **Receipt success is distinct from integrity verification.** A mined transaction
   can coexist with failed or incomplete read-back verification.
3. **Outer report fields are not covered by the record hash.** Changing
   `face_match_verification` without changing `record` leaves `record_hash` unchanged.
   The suite documents this boundary; it does not add protection for those fields.
4. **Read-back summaries reuse prior history.** Detection, source review and the
   comparison row are marked as saved-run results rather than rerun stages.
5. **Social filtering is about source-page links.** Thumbnail URLs may point to
   provider/CDN domains even when all accepted page URLs are social-domain links.

## What remains unvalidated

| Area | Limit of this test run |
|---|---|
| Recognition models | No model downloads, checksum verification, model loading, encodings, threshold testing or identity comparisons were executed. |
| Candidate-image retrieval | No candidate thumbnail or social-page image was fetched or authenticated. |
| Positive detection | No photograph containing a consenting subject was tested here. |
| External search | No authenticated ImgBB or SerpAPI call, expiry audit or live social-post review was performed. |
| Public blockchain | No live Sepolia RPC, broadcast, successful receipt or successful on-chain read-back was exercised. |
| Recovery/network faults | No public receipt timeout, actual RPC rejection, reorg or provider outage was reproduced. |
| Complete CLI run | Full image-to-chain execution was not run; the tests call selected functions and parsers. |
| Terminal rendering | Rich output was captured and checked; Windows font rendering and all interactive terminal variants were not visually tested. |

Offline signing is real cryptographic signing, but it is not evidence that a node
accepted a transaction. The local-tamper test rejects a modified record before an
RPC call; it does not demonstrate successful verification against a public chain.

The author previously reported successful image upload/search and a gas-estimation
failure on their machine. These are user-reported observations and are not counted
as independently reproduced test passes here.

## Submission evidence status

The updated README provides slots for five stage screenshots, a summary panel and
an explorer view. No screenshot or unedited recording was provided for this test
update. Do not label those slots as completed evidence until the actual artifacts
have been attached.

For already-existing blockchain evidence, record the actual transaction ID,
network, receipt outcome and read-back result separately. Never publish credentials
or report a local UI fixture as a successful transaction.

**This validation is a bounded regression check of non-biometric components. It
is not an accuracy claim, an identity-verification certification, or proof of an
end-to-end live deployment.**
