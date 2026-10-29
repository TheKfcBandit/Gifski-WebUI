# Contributing

Gifski WebUI uses Python's standard library and plain HTML, CSS, and JavaScript. Keep the source launcher portable: writable settings and generated files belong under the application's runtime folder, never browser storage or the current working directory.

## Local checks

Use Python 3.10 or newer. Native Gifski, FFmpeg, and FFprobe are needed for real encoding, but most unit tests use controlled fixtures.

```sh
python -m unittest discover -v
node --check static/app.js
```

Node.js is only needed for the JavaScript syntax check. CI tests Windows and Linux on Python 3.10, 3.13, and 3.14. The checks workflow validates source; it does not produce an executable release.

For UI changes, verify narrow and wide layouts, keyboard controls, request failures, saved-preset round trips, and progress after closing and reopening Settings. For engine changes, test a short real encode and relevant cancellation or failure paths. On Windows, compare canonical paths or file identity: a temporary directory can have both an 8.3 alias and a long name.

## Changes and reports

Keep changes focused and explain the resulting behavior and validation. Follow [DESIGN.md](DESIGN.md) for the interface flow and [FEATURES.md](FEATURES.md) for encoder coverage. Use direct technical labels and progressive disclosure; avoid duplicate controls.

Never include personal media, `data/`, `bin/`, absolute local paths, tokens, or unredacted logs in a commit or report. A small generated test clip is preferable to a private input file. Report bugs with the app revision, OS, Python/encoder versions, reproduction steps, and a redacted error.

