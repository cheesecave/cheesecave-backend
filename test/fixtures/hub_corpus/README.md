# Hub corpus

A frozen sample of real Hugging Face Hub repositories, read by the downstream-library tests
(`test/kohakuhub/api/test_downstream_*.py`). Nothing here is fetched while tests run.

## What is in it

- `manifest.json`: for every repository its id on the Hub, the **revision** it was sampled at, its
  **license** as the card declares it, every kept file with its size and sha256, and what the Hub said
  about it then (`hub`: pipeline tag, library, tags, parameter count).
- `repos/<model|dataset>/<owner>__<name>.zip`: the files of each repository, byte for byte, in one zip
  (so git holds a few hundred objects, not thousands). Tests release a zip into a temporary directory
  when they need it; `manifest.json` has the hash of every file inside.

Kept: configs, tokenizer files, cards, indexes and small tables (parquet, csv, json, jsonl, txt).
**Never kept: weights and any other common binary format.** A `.safetensors` file is kept as its header
only (the 8-byte length and the JSON header; the tensor data is cut off), which is all that
`get_safetensors_metadata` and friends read. Such a file is not loadable and tests do not load it.

## License

The files belong to their authors and keep **their own licenses**, which differ from repository to
repository and are recorded per repository in `manifest.json`. They are here only as test input for
this project's compatibility tests and are not part of its own licensed code; anyone reusing them must
follow the license of each original repository.

## Refreshing

Only by hand, slowly, with the revisions pinned: `python scripts/corpus/sample.py` (see
`scripts/corpus/`). CI never talks to the Hub; the tests fail if a file does not match its hash.
