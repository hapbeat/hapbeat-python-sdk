# Releasing hapbeat (PyPI)

## 0. Gate — verify on a real device first

This is the hard prerequisite. Publishing the SDK before it is confirmed to
drive a physical Hapbeat is against the project's distribution policy.

```bash
pip install -e .
# Power on a Hapbeat on the same LAN; deploy a kit via Hapbeat Studio.
python - <<'PY'
import hapbeat, time
hb = hapbeat.connect(app_name="ReleaseCheck")
print("devices:", hb.discover(1.5))
hb.play("impact.hit", gain=0.5)   # use a real event id from your kit
time.sleep(1); hb.stop_all(); hb.close()
PY
```
Confirm the device actually buzzes. Only then proceed.

## 1. One-time setup (PyPI Trusted Publishing — no tokens)

1. PyPI -> https://pypi.org/manage/account/publishing/ -> **Add a pending publisher**
   - Project: `hapbeat`  ·  Owner: `hapbeat`  ·  Repo: `hapbeat-python-sdk`
   - Workflow: `publish.yml`  ·  Environment: `pypi`
2. Same on https://test.pypi.org for Environment `testpypi` (optional, for dry runs).
3. GitHub repo **Settings -> Environments** -> create `pypi` and `testpypi`.

(The name `hapbeat` was free on PyPI as of 2026-06-01.)

## 2. Release

```bash
# bump version in pyproject.toml (e.g. 0.1.0)
git commit -am "release: v0.1.0"
git tag v0.1.0
git push origin master --tags
```

The tag push runs the `publish.yml` workflow: it runs `pytest` (gate), builds,
and publishes to PyPI via OIDC. A `vX.Y.Z-rcN` / `aN` tag goes to TestPyPI.

### Manual fallback

```bash
python -m build && python -m twine upload dist/*
```

## Note — dependent package

`hapbeat-vrchat` depends on `hapbeat`. Publish **this** package first; only then
can `hapbeat-vrchat` resolve its dependency on PyPI.
