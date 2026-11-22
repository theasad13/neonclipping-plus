# Changes in this fork

Original project: [iamajmeena/neonclipping](https://github.com/iamajmeena/neonclipping) by @iamajmeena, licensed MIT.

## Added in this fork

- **Batch mode from the command line (`batch.py`).** Clip a whole list of links in one go: it sends each link to the
  running app, waits, and saves every clip with its `.srt` captions plus `clips.csv` / `clips.json` (title, hook,
  score, start, end). Re-running with the same `--out` folder skips links that are already done, so long lists can be
  stopped and continued. Standard library only, tested in `tests/test_batch.py`.
