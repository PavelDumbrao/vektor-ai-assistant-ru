# hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2-lm2

Живой релиз на VPS (снят 2026-10-22). Upstream: `29112bef099274229cadff79cdff7bf7b99c4b77` (tar sha256 `b80dc110dc3f7fcbf7978f5f7c82c99022671acd2cdc7fdbd6ec5bfae6409e2a`), слои: `vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2-lm2`.

- `composite.patch` — полный diff upstream → `hermes-agent/` релиза (45 файлов; исключены `__pycache__`, `*.pyc`, `website/`). Проверка: `patch -p1` на чистый upstream даёт дерево, идентичное релизу (`diff -rq` пуст).
- `runtime.json` — хэши (`code_sha256` `b52ce6e9817bcd763d6f26f257b983e2a2d18412079577ca9a8867a3d3cefb7c`, `dependencies_sha256` `109fd8cd13df8d819a7ffe9aa057f8477ac999c0160de19c7faa9b86f32ab66b`), `parent_release_id` = `hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2`.
- `code-manifest.json` — sha256 каждого файла дерева релиза.
- Последний слой (`verification`): `{'purpose': 'long_media_transcription_lm2', 'source_commit': 'd3a4d77bbef25e4ad5eff4220f985d55c7ba27c6', 'ported_from_live_v2_parent': True, 'dependencies_identical': True, 'changed_files': ['tools/long_media_transcription.py', 'tools/transcription_tools.py', 'gateway/run.py'], 'parent_verification'`

Пересборка: чистый checkout upstream → `patch -p1 < composite.patch` → `prepare_release.py --archive … --patch composite.patch --release-id hermes-0.21.0-29112bef-vektor3-pavel-tglf2-vtm1-tgrel8-pmfix2-pi2-lm2`.
