## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `pytest -m "not network and not models"` and `ruff check snipmint tests scripts` pass
- [ ] Behaviour changes have tests
- [ ] New dependencies, fonts or models are listed in `THIRD_PARTY_LICENSES.md` and allow commercial use
- [ ] API changes: ran `python scripts/export_openapi.py && (cd frontend && npm run api:types)`
- [ ] UI changes: `npm run typecheck && npm test` pass, and I checked it at phone width

## Manual check (for voice, timing or caption changes)

- [ ] I listened to the result
- [ ] Captions stay in sync (play the video with `captions.srt` or watch the highlighted word)
- [ ] Preview frame looks right in the Captions step
