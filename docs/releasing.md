# Python release preparation

The initial intended destination is `Nitmi/lab-instruments`, tag `v0.8.0`.
Until a successful tag workflow and independent verification exist, this remains
a local software candidate. Do not describe Windows/SDK/instrument measurement
tests as passed because local protocol, C ABI or synthetic thermal doubles pass.

The four Python assets are the wheel, source archive, `release-manifest.json`
and `SHA256SUMS`. Build/verify are development-only; they are not runtime imports.
No vendor library, driver or compiled thermal helper is bundled. The sdist carries
the C++ source and explicit build instructions. CI tests a locally built helper
only through its synthetic backend; a user builds/selects a trusted helper before
separately authorized physical camera work.

```powershell
python scripts/release.py build --output-dir '<new-directory-outside-checkout>'
python scripts/release.py verify '<release-directory>' --source-revision '<40-hex-commit>'
```

Build requires clean committed source and an existing locked build environment.
Hosted builds explicitly use Hatchling's default `SOURCE_DATE_EPOCH=1580601600`;
use the same value when comparing local and hosted assets. Older local candidate
ZIP timestamps can differ while their installed payload bytes remain identical.
`--tag v0.8.0` additionally requires that exact tag resolve to the same commit and
version before and after build. Output must be new and outside the checkout.
The source archive must equal all tracked files plus generated PKG-INFO, and all
runtime wheel files must equal source bytes. Wheel identities, exact inventory,
entry points, metadata, RECORD and runtime dependency absence are validated.
Verify does not execute or extract archives and does not establish a publisher.

The tag workflow calls CI, binds the trigger commit, builds twice, compares all
four assets, cold-installs that wheel, attests it and uploads the results.
It has no Release/PyPI publishing step or contents-write permission. Human/task
release coordination verifies the downloaded assets and source before creating
the GitHub Release. Keep tags immutable; do not replace a failed published tag.

Independent verification uses the exact repository, source ref, source digest,
signer workflow/digest and hosted-runner requirement, for example:

```powershell
gh attestation verify '<asset>' --repo Nitmi/lab-instruments `
  --source-ref refs/tags/v0.8.0 --source-digest '<commit>' `
  --signer-workflow Nitmi/lab-instruments/.github/workflows/python-release.yml `
  --signer-digest '<commit>' --deny-self-hosted-runners --format json
```

Apply this to all four exact assets. Then run the source-bound static verifier
from trusted code, install only the verified wheel into a new isolated environment
with `uv pip install --no-index --no-deps`, and inspect installed identity/payloads.
No global PATH, driver, station lock or board access is part of this setup.

The verifier bounds compressed files at 8 MiB, wheel payloads at 8 MiB/128
members and the inflated tar stream before parsing metadata at 8 MiB plus 512 KiB;
source payloads are at most 8 MiB/256 members. Parsing/hashing is proportional to
those bytes. Parsed members and tar buffers add memory, so these are not RSS caps.

References: [GitHub attest action](https://github.com/actions/attest),
[GitHub CLI verification flags](https://cli.github.com/manual/gh_attestation_verify).
