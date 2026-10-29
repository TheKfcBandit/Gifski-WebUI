# Gifski feature reference

Audited against the published Gifski 1.34.0 command-line source. The app checks the installed executable separately; a different build can expose different capabilities. Source references are below.

## Complete CLI inventory

| Option | Values / default | Meaning |
|---|---|---|
| `--output`, `-o` | Required path; `-` = stdout | GIF destination |
| Positional files | PNG sequence; single Y4M/GIF/video*; `-` = stdin | Source |
| `--fps`, `-r` | Float, >0–100; 20 | Frame rate |
| `--fast-forward` | Float, >0–1000; 1 | Playback multiplier |
| `--quality`, `-Q` | Integer 1–100; 90 | Overall quality |
| `--motion-quality` | Integer 1–100; inherits quality | Temporal detail |
| `--lossy-quality` | Integer 1–100; inherits quality | Lossy compression |
| `--fast` | Boolean; off | Faster encoding |
| `--extra` | Boolean; off | Additional effort; conflicts with fast |
| `--width`, `-W` | Unsigned integer | Maximum width |
| `--height`, `-H` | Unsigned integer | Maximum height |
| `--repeat` | -1 / 0 / 1–32767; 0 | Once / indefinitely / repeats |
| `--bounce` | Boolean; off | Forward/reverse PNG sequence |
| `--no-sort`, `--nosort` | Boolean; off | Preserve supplied order |
| `--fixed-color` | Repeated six-digit RGB hex; optional `#` | Retained palette colors |
| `--matte` | Six-digit RGB hex; optional `#` | Partial-alpha background |
| `--y4m-color-override` | bt709, fcc, bt470bg, bt601, smpte240, ycgco | Y4M matrix |
| `--quiet`, `-q` | Boolean; off | Suppress normal console output |
| `--help`, `-h` | Long / short help | Usage and capabilities |
| `--version`, `-V` | Flag | Version |

*Direct conventional video decoding requires Gifski's optional video build feature. Using the separate FFmpeg executable removes this requirement. PNG input requires multiple images. Fixed colors accept space/comma lists and repeated flags. Long help includes options omitted from short help.

## Behavior that affects results

- PNG frame rate sets timing without discarding frames. Video rate resamples motion. Gifski sorts frame paths naturally unless sorting is disabled. Native bounce applies only to individual PNG files; it is ignored for a single video/pipe. The wrapper can implement equivalent behavior before encoding.
- The native encoder never enlarges images. One maximum dimension preserves proportions; two can stretch. Omitting both enables automatic downscaling around an 800 × 600 pixel budget. Explicit processed dimensions avoid a second resize.
- GIF stores centisecond timing. Gifski clamps each written delay to at least 2 centiseconds: settings above 50 FPS cannot produce equally fast GIF playback. Fractional rates produce uneven frame delays.
- Matte blends semitransparent pixels; fully transparent pixels remain transparent. Without matte, alpha is converted to binary transparency. Y4M has no alpha: use RGBA PNG frames for transparent material.
- Fixed colors are capped at 255, reserving one palette entry for transparency. They can increase size. Additional encoding effort may improve appearance while increasing size; it is not a guaranteed compression improvement.

## Quality versus size

Resolution and duration are powerful size controls. Lower lossy quality adds noise/streaks; lower motion quality sacrifices temporal detail. Preserve these as separate controls when testing combinations. Output-size estimates are approximate. A target-size search must measure complete resulting GIF files, keep each candidate's settings, and report when no tested candidate meets the budget. Presets are starting points, not guaranteed optimums.

FFmpeg preprocessing provides trim, crop, scale algorithms, FPS conversion, timestamp/speed adjustment, rotation, reversal, color correction, and denoising. These are separate from Gifski encoding options. The `fps` filter drops/duplicates frames; `setpts` changes timing. Trimming before reversal limits buffering. Mild denoising may help noisy footage compress, but it can remove detail.

## Deliberate boundaries

Gifski's CLI has no independent dithering-strength, palette-count, thread-count, or resize-algorithm option. Do not present invented Gifski flags for these. Its library APIs additionally accept raw RGB/RGBA buffers, per-frame timestamps, and progress/write callbacks; these are integration interfaces, not extra CLI sliders. The optional lossy-compression build feature can affect whether lossy-quality changes have an effect. This wrapper uses local executables, not a recompiled library.

## Official references

- [Published CLI source, Gifski 1.34.0](https://docs.rs/crate/gifski/1.34.0/source/src/bin/gifski.rs)
- [Encoder implementation](https://docs.rs/crate/gifski/1.34.0/source/src/lib.rs)
- [PNG timing](https://docs.rs/crate/gifski/1.34.0/source/src/bin/png.rs)
- [Y4M decoding](https://docs.rs/crate/gifski/1.34.0/source/src/bin/y4m_source.rs)
- [Upstream usage and size guidance](https://github.com/ImageOptim/gifski)
- [C API](https://github.com/ImageOptim/gifski/blob/main/gifski.h)
- [Release downloads](https://github.com/ImageOptim/gifski/releases)
- [FFmpeg filter reference](https://ffmpeg.org/ffmpeg-filters.html)

## WebUI decision tools

- **Source matching:** width/height/FPS and trim-end controls can follow probed source values. Dimension matching clears the other dimension to preserve aspect ratio. Source FPS matching caps at 50 FPS for GIF timing.
- **Size estimate:** complete measurement for short clips; three separated sample encodes for longer clips. Displays a heuristic range and marks estimates stale after recipe changes. Samples are explicitly identified and are not full exports. Unknown source duration requires a trim end. Long native-direct input cannot be sampled without preprocessing.
- **Discord 20 MB:** derives aspect-preserving dimensions and FPS from the source, limits its starting bounding box to 1280 pixels per axis and 50 FPS, then compares full encodes. The exact acceptance threshold is 20,000,000 bytes. Quality and effort are adaptive (normal effort, inherited motion/lossy quality), while source, trim, crop, image filters, and loop behavior are preserved. Ranking uses a transparent settings preference, not a measured perceptual score. All completed alternatives remain available.
- **Setup:** detects existing tools and offers executable-path selection. Windows x64 portable installation downloads only from fixed providers, verifies SHA-256, and writes only to the local app folder. Python must already be installed to launch the wizard.
