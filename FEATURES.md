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
| `--fast` | Boolean; off | Fast effort mode |
| `--extra` | Boolean; off | Extra effort mode; conflicts with fast |
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

Normal, Fast, and Extra are the three CLI effort modes: Normal omits both effort flags. The UI’s Smart search effort is a separate candidate-search budget, not another Gifski effort level.

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

- **Three modes:** Default selects built-in or saved recipes; Smart fits a file-size budget; Advanced exposes manual encoder and preprocessing controls. Default provides an Edit in Advanced action. Shared clip/playback/output controls stay below the mode choice. There are no separate Custom, Other presets, Discord, Create, History, Compare, or Fit a size modes/tabs.
- **Workspace:** controls on the left; process status, logs, results, and history on the right; Settings in a dialog. The fixed footer holds estimates and encoding actions. The layout stacks on smaller screens. Small static cached history thumbnails avoid downloading every full GIF; thumbnail failure does not fail an export.
- **Recipes:** Balanced, Compact, Fidelity, UI & text, Motion, and Transparent are starting points within Default. Saved recipes include `mode`; Smart restores its target, effort, and pins. Fixed recipes load in Default and can be edited in Advanced. Imports migrate old fixed-mode records to Default and legacy Discord recipes to Smart at 20 MB. Portable recipe exports exclude media paths.
- **Source matching:** width, height, FPS, and trim-end controls can follow probed values. Matching one dimension clears the other to preserve aspect ratio. FPS matching caps at 50 FPS.
- **Size estimate:** full measurement for clips up to two seconds; three separated samples of about half a second for longer clips, with at least four frames each. Expandable footer details show a heuristic range and cached/stale/calculating/error states without page scrolling. Unchanged recipes reuse a five-minute memory cache keyed by settings, source metadata, and executable selection. Samples are never verified exports. Unknown duration requires a trim end; long native-direct input requires preprocessing to sample.
- **Smart limit:** editable decimal MB, with Linked embed · 10 MB and Discord upload / preview · 20 MB shortcuts. The 10 MB option is conservative guidance from an older Discord page, not a verified separate current maximum. The newer attachment FAQ documents 20 MB free uploads and direct image preview behavior. See [README.md](README.md#sharing-size-shortcuts) for dated sources and limitations. Legacy API mode `discord` remains a fixed 20,000,000-byte Smart search.
- **Smart pins:** `smart.pinResolution` and `smart.pinFps` are booleans using the ordinary `width`/`height` and `fps` settings. A pinned resolution requires at least one dimension; one preserves proportions, two can change them. Effective dimensions are limited to the cropped source without upscaling and remain fixed during search. Pinned resolutions over 16 megapixels are rejected. Pinned FPS remains fixed and must respect the 50 FPS ceiling; PNG sequence timing includes playback speed. The search never relaxes a pin to claim a fit.
- **Adaptive search:** unpinned axes start at source dimensions within a 16-megapixel safety bound and source timing up to 50 FPS. Probe quality and unpinned spatial/temporal changes to estimate size impact, then screen promising combinations before full verification. Quick allows up to 5 sample recipes / 4 full encodes, Balanced 9 / 8, and Thorough 15 / 12; each may stop earlier. Thorough can test Extra effort on a fitting result. Each sample recipe uses up to three short encodes.
- **Ranking:** width retention has weight 0.75 and FPS retention 0.30; quality follows a diminishing-return curve near 100. Extra gets a small preference bonus only after its complete output fits. A predicted gain below 0.8% can stop refinement. This is a settings preference, not a perceptual metric or exhaustive optimum. Source, trim, crop, image filters, and playback stay intact; the search never silently shortens a clip.
- **Verification and resources:** only full outputs at or below the exact byte limit qualify. Cancellation retains completed candidates. No-fit is explicit, including when pins prevent further reduction. Compatible PNG preparation is reused within a job, capped at 512 MiB cached / 256 MiB per entry, then removed. More than 100,000 extracted frames is rejected rather than silently truncated.
- **Live feedback:** process stage, elapsed time, and available frame/FPS/speed data. Structured FFmpeg progress and meaningful Gifski PNG frame counts drive percentages; unknown progress remains indeterminate. Carriage-return output is drained continuously. Bounded logs include process start/exit and periodic keepalives even in quiet mode. Follow-log can be disabled. Cancel terminates tracked processes, including job metadata inspection, with kill escalation. Gaps in output are diagnostics, not fabricated failures.
- **Efficient updates:** compact summaries plus selected-job details, non-overlapping requests with deadlines, slower hidden-page polling, reconnect feedback, and stable history/log nodes. A separate pending estimate can update the footer while another job is inspected. Static thumbnails prefer a finished output and fall back to a source frame at the trim start for estimate/other terminal history. Cached JPEGs are limited to 160 × 90 pixels, 128 KiB, and five seconds of preparation; access is authenticated and failure leaves a placeholder.
- **CPU and hardware:** Advanced offers FFmpeg threads `0–256` and CPU or automatic hardware video decoding. Zero keeps decoding automatic and bounds filter/PNG workers to eight or the logical CPU count, whichever is lower. Explicit thread requests apply to decoding, filters, and PNG preparation. Hardware decoding can retry on CPU; built-in filters and Gifski remain CPU work. Gifski manages its own threading. Direct mode bypasses FFmpeg.
- **Browser enhancement:** optional screen wake lock during active jobs while visible and supported. An unavailable/rejected wake lock does not block encoding or guarantee system wakefulness.
- **Dependency setup:** explicit paths override automatic detection; PATH precedes app-local copies. FFprobe can also be found beside configured FFmpeg. Active versions, paths, and origins are visible. Windows x64 Set up missing tools rechecks executables and downloads missing tools into `bin/` using fixed providers and SHA-256 checks. Downloads require an action; Python must already be installed. Installs and path changes cannot overlap active exports. Restart the app after changing system PATH.
- **Version checks:** asynchronous fresh local detection followed by upstream comparisons; successful upstream metadata can be reused for 15 minutes. Gifski’s API can fall back to its release page; FFmpeg’s archive can fall back to stable tags from its official mirror. Results arrive independently and include active path/origin, build kind, comparison scope, update method, and actionable provider diagnostics. Recognized stable vendor builds compare against their upstream release base; vendor revisions remain outside that comparison. Git/nightly/development/prerelease/unrecognized builds have explicit uncertain states. Installing an app-local copy does not replace or switch a working PATH or explicit executable.

Older sweep/optimize/discord API modes remain readable for existing history and integrations; they have no separate interface workspace. See [DESIGN.md](DESIGN.md) for the layout philosophy and boundaries.
