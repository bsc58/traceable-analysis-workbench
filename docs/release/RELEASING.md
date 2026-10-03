# Offline release preparation

The selected license is MIT. The intended repository is `traceable-analysis-workbench`, initially private. This document does not authorize an upload: the updated candidate needs read-only review and an explicit subsequent upload instruction.

Only the public release directory may be published. Never upload the development repository or its history. Package into a new directory, retain the root MIT `LICENSE`, and refresh `PUBLIC_MANIFEST.json` against the exact copied bytes. The current packaging implementation predates the license decision and does not select a root `LICENSE` on its own; copy that file explicitly and include its SHA256 in the manifest with `license_status` set to `PASSED`. The obsolete pending-license notice must be absent. No runtime or packaging code was changed for this documentation/license step.

Run the content scan and release-tool probes:

```sh
python scripts/release_scan.py NEW_DIRECTORY --report /path/outside/release-scan.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=NEW_DIRECTORY/src python NEW_DIRECTORY/scripts/demo_checks.py
```

The scanner rejects recognized local paths/usernames, sensitive markers, common key formats, credential files, database artifacts, unknown binary files, symlinks and files above 5 MiB. PNG/GIF/MP4 demo media have signature checks. A passing scan is a bounded check, not proof that every possible secret encoding is absent.

After the owner has authenticated the GitHub CLI, obtain identity using `gh api user`: use the returned login as copyright holder and Git author name, and construct the author/committer email as `<id>+<login>@users.noreply.github.com`. Do not infer identity from a local username or existing Git configuration. Until authenticated identity is available, the copyright holder remains `TBD` and the final author commit is not complete.

The repository check in the unchanged scanner's `--git` mode still expects the old placeholder author. For this owner-authored candidate, run the content scan without `--git` and verify repository identity separately against the authenticated API result: exactly one root commit, matching author and committer, no parent, no remotes, and a clean working tree. Record both checks in the external publish checklist; do not describe the old placeholder-author gate as passing for a different author.

For the never-pushed, single-commit candidate, replace the initial commit only after reviewing the exact staged files. Keep the repository at one root commit with no remote. Record every selected file hash, the generated manifest hash, the authenticated identity, and the resulting commit in the publish checklist outside the release directory. Confirm each copied file matches its development source byte for byte; `PUBLIC_MANIFEST.json` is generated release metadata and does not hash itself.

No database, hidden answer file, credential, dependency installation, or development evidence belongs in the release. Do not add a remote, create a hosted repository, or push during this preparation step.

## Documentation updates after publication

Once the repository is published, preserve its history and use normal follow-up commits. Push only the public release checkout. The initial single-root-commit and no-remote checks above apply to the original, never-pushed candidate, not subsequent updates. Verify the current authenticated owner, expected remote, clean state and pushed commit separately from the content scan.

Keep `README.md` (English), `README.zh-CN.md` (Chinese) and both report images in sync with the development source. The original packaging allowlist does not select `README.zh-CN.md`; copy it explicitly, along with the root MIT `LICENSE`, and include their hashes in `PUBLIC_MANIFEST.json`. The English report image is an AI-translated documentation illustration of the original Chinese screenshot, not evidence that English UI localization is implemented. Preserve the original screenshot and label the translation in both READMEs.
