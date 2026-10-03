# Verification scope

Implementation self-check, not independent acceptance. Measured on macOS arm64 in a new Python 3.12.14 venv and isolated Node 24.19.0 / npm 10.9.3 installation. Dependencies came from `requirements.lock` and `apps/web/package-lock.json`; pip 25.0.1, setuptools 75.8.0 and wheel 0.45.1 were pinned. No existing Python or web dependency directory was reused.

| Check | Result | Scope |
| --- | --- | --- |
| README dependency installation and web build | PASSED | Fresh environment; editable package installed without dependency resolution/build isolation |
| No-key `scripts/demo.sh --fixture-only --test-instance --no-open` | PASSED | Create → fixture executor → published complete report, six accepted facts; structural validation warning correctly shown |
| `python scripts/demo_checks.py` | PASSED | 6 release/demo contract probes |
| `npm --prefix apps/web test` | PASSED | 12 component tests |
| `python scripts/demo_test.py --output EXTERNAL_DIRECTORY` | PASSED | 567 passed, 29 skipped, one dependency deprecation warning; fresh empty guarded test instance |
| Playwright recording | PASSED | Six views, UI-created run succeeded, compatible comparison; zero external requests and zero page errors |
| Cleanup | PASSED | Both demo and subsequent regression instance shut down and removed, including sockets |
| Docker startup | NOT RUN | Docker unavailable; images were not pulled |
| Optional real provider | NOT RUN | No paid request in release preparation |
| Hidden-answer-dependent tests | NOT RUN | 28 skips; reference answers deliberately absent |
| Separate business-package test | NOT RUN | One skip; no package contents read |
| Full 58 acceptance criteria | NOT RUN | Ordinary tests and these bounded probes do not establish full acceptance |

The first regression attempt incorrectly shared an instance with the seeded demonstration. The migration test correctly refused a destructive downgrade while execution rows existed; later tests then encountered the older schema. That failed attempt (67 failed, 489 passed, 19 skipped, 112 errors) is retained in internal raw evidence. The successful count above is a new run on an empty instance, not a filtered subset or a change to runtime behavior. Tests were not weakened to resolve that setup failure.

The demo and regression commands use the approved release-preparation test directory/database pair. They must run sequentially. For an ordinary public checkout, use the README demo command without `--test-instance`; it owns a disposable local demo instance. The regression guard intentionally refuses arbitrary database URLs.

The distributed recording is a silent 25.16-second H.264 MP4, 1280 × 800 at 25 fps, with BT.709 tags and the original screen aspect. Format/size checks passed; absent audio and subtitle tracks are reported as optional warnings. A six-frame contact sheet was inspected: all six views are legible, and report validation scope is displayed above the title. No model-quality inference is attached to the recording.

Raw installation logs, failed and successful pytest output/JUnit, browser assertions, video probe/check output and cleanup records are retained outside the public package. Independent review remains NOT RUN; integrated measured evaluation is linked below.

## Final integrated Batch 2 checks

The merged code passed 647 public Python tests (one separate business-package test explicitly deselected), 12 UI tests and six release contract probes. A second run in the clean installation environment, with both reference directories explicitly absent, passed 609 tests with 39 skips: 28 previous hidden-reference probes, ten new evaluation-reference probes and one separate-package probe. Skips remain NOT RUN, not passing results. Migration rollback probes run first on the empty instance; the supplied regression launcher now enforces this order.

The [small holdout evaluation](../../eval/results/b2/README.md) completed all 225 planned trials; its low model completion scores are retained. All 225 stored public run histories and exact trial receipts were reopened in a new process. No database snapshot or database copy is shipped. The final UI build matched the recorded demonstration assets. Docker, optional real-provider demo, full acceptance and independent release review remain NOT RUN.

The final no-key demo was started again on a disposable guarded instance: six structured facts published, local web page returned HTTP 200, and shutdown removed the instance. The phase verifier passed 37 recorded groups after cleanup; deployment/media files also have a supplemental hash inventory. These records are implementation self-checks, not independent acceptance.

All five original browser end-to-end tests were also rerun on the merged tree using only the approved synthetic test instance. Six-view creation, server refusal, blocked reports, mobile layout, gateway boundaries and browser credential exclusion passed. New screenshots were retained outside the public package; historical evidence files were restored unchanged.
