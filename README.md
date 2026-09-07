# 7SEPT

Three files:

1. **`utils/validation_step.py`** — resolution tracking (`_build_resolution`, `_extract_checks`, the new payload fields)
2. **`utils/config_loader.py`** — added the `sync_enabled` field
3. **`config.yaml`** — `sync_enabled: false`
4. **`pipeline.py`** — sync now gated on `sync_enabled` rather than just `source_bucket`

Files changed: utils/validation_step.py and validation_agent.py.
