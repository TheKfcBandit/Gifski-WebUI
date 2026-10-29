# Gifski WebUI design

## Interface principles

The task is to make a useful GIF. Organize the interface around three decisions: start from a recipe, fit a size, or control the encoder manually. These map to **Default**, **Smart**, and **Advanced**. Recipes and sharing limits are choices within those modes, not additional modes. A complete result’s measured byte count determines whether it fits.

Use progressive disclosure without duplicating controls. Default selects a starting recipe; shared clip, playback, and output controls remain visible. Smart adds a size budget and optional constraints. Advanced exposes all manual encoder and preprocessing controls in named groups. Changing modes preserves the working clip and relevant settings; switching a panel should not silently clear a pin or change the source.

Keep progress beside the recipe. A persistent activity column holds status, logs, output, and history. The bottom toolbar holds estimates and the primary action. Occasional configuration belongs in Settings. Dependency onboarding only establishes working tools; it does not ask for a sharing preset or start downloads without an install action.

## Choice and layout plan

The desktop workspace has controls on the left and an independently scrollable activity column on the right. On smaller screens the activity column follows the controls. The fixed toolbar’s measured height reserves space below the page.

1. **Source:** file/path choice, preview, metadata, and change-source action.
2. **Mode → Default:** compact recipe selector containing built-in and saved recipes; an Edit in Advanced action for detailed tuning.
3. **Mode → Smart:** editable size limit; Linked embed · 10 MB and Discord upload / preview · 20 MB shortcuts; search effort; optional resolution/FPS pins with dependent fields shown only when enabled.
4. **Mode → Advanced:** manual dimensions, FPS, quality, and effort; disclosures for image processing, motion/compression/palette, input/diagnostics, performance/hardware, and command preview.
5. **Shared clip and output:** trim, playback, and file name. Source-match actions stay beside the corresponding fields. Recipe import/export stays available; saving belongs with Smart and Advanced editing.
6. **Toolbar:** output summary, estimate summary, and one primary Encode GIF or Find best fit action. Estimate details expand above the toolbar. Estimates do not create a competing results panel or scroll the page.
7. **Activity:** selected process and stage, measurable progress or an indeterminate state, elapsed time, frame/rate information, cancel, and visible logs with optional follow mode.
8. **Output and history:** selected full GIF, measured size, recipe, download, and collapsed alternatives; recent jobs have small static thumbnails. Selecting history updates inspection without scrolling the page or replacing focused controls on every poll.
9. **Settings:** installed tools, paths and origins, online version checking, portable installation, CLI help, and optional browser wake lock. No Create, History, Compare, or Fit a size navigation tabs.

Built-in and saved recipes use the same selector. Saved Smart recipes restore the budget and pins and open Smart. Fixed recipes load in Default, with deeper editing available in Advanced. Recipe JSON records `mode`; imports accept older records and map legacy fixed modes to Default and legacy Discord to Smart with a 20 MB target. Keep recipe media paths out of portable exports.

## Size limits and constraints

Sharing shortcuts fill the same editable decimal-MB field. The 10 MB linked-embed value is a conservative compatibility target from older Discord guidance. The 20 MB upload/preview value follows the newer attachment FAQ. Do not present them as two independently verified current platform maxima or guarantee that a host/account will preview a file. Keep the source dates and uncertainty in concise help rather than adding more mode buttons.

Pins are constraints, not ranking preferences. With resolution pinned, every candidate uses the same effective requested dimensions, capped to the cropped source without upscaling. One dimension preserves aspect ratio; two independently set dimensions can change it. Reject pinned dimensions above the Smart safety limit instead of shrinking them during search. A pinned FPS remains fixed, including documented PNG playback-speed semantics. If pins leave no tested fit, show no-fit and suggest an explicit user choice: release a pin, shorten the clip, or raise the budget.

## Search principles

- Preserve the chosen clip, crop, playback, and image processing. Never silently trim to fit.
- Start from source detail and timing without upscaling, within resource limits. Only unpinned axes may change.
- Screen representative temporal samples before full exports. Cache unchanged estimates and reuse compatible prepared frames. Samples guide selection; they never establish a fit.
- Test significant resolution/frame-rate changes and moderate quality changes before small refinements. Skip pinned axes entirely.
- Rank retained spatial detail, motion, and quality with an explicit settings score that has diminishing returns near quality 100. Thorough may verify Extra effort on a fitting result. This is a heuristic preference, not a measured perceptual metric or a global optimum.
- Verify shortlisted candidates with full encodes. Only actual byte counts at or below the limit qualify. Stop when improvement is small or the effort budget is spent; retain completed candidates on cancellation and report no-fit honestly.
- Bound sampling, candidate counts, temporary data, and process lifetime. Prefer a clear failure to a silently truncated export.

## Responsive feedback and thumbnails

FFmpeg reports structured progress. Gifski’s carriage-return output is drained continuously and sanitized; meaningful PNG frame counts can supply a percentage, while unknown progress remains indeterminate. Start/exit messages and periodic keepalives remain available with quiet output. A gap in reports is uncertain activity, not an automatic failure. The wrapper drains both sides of a pipe and terminates a waiting peer when its producer or consumer fails. Job metadata inspection participates in cancellation.

Poll compact history summaries and details for the selected job and any separately pending estimate. Slow polling in hidden documents. Requests have deadlines, overlapping polls are prevented, and failed updates show a retry state. Reuse history nodes and leave unchanged logs/previews intact. A request timeout or hidden tab does not cancel encoding.

History thumbnails are static cached JPEGs within 160 × 90 pixels. Prefer a finished output; otherwise use a source frame at the selected trim start, including estimate history. Limit generation to five seconds and 128 KiB. This work is separate from export correctness; missing inputs or failures leave a placeholder. Serve them through authenticated job access without exposing arbitrary source paths. Do not use a full animated GIF as the history thumbnail fallback. The selected result can load its full preview independently.

Screen wake lock is optional. Request it only while a job runs and the document is visible, release it when no longer needed, and handle unsupported/rejected requests. It is not a guarantee against system suspension.

## Performance controls

Distinguish video decoding, image preparation, and GIF encoding. Optional FFmpeg hardware decoding depends on the source, build, and driver and can retry on the CPU. Built-in filters and Gifski encoding remain CPU work. FFmpeg thread settings apply to decoding, filters, and PNG output; automatic filter/PNG workers are bounded to eight or the detected CPU count, whichever is lower. Gifski retains its native CPU parallelism without an invented CLI thread slider. Direct input bypasses FFmpeg.

## Dependency and version model

Show the executable actually in use, its detection origin, and its reported version. Explicit paths take priority; automatic detection prefers PATH before app-local copies. FFprobe may be discovered beside configured FFmpeg. Restart is necessary to inherit system PATH edits made after launch. Missing-only setup rechecks tools before downloading; an invalid explicit override remains visible rather than being silently replaced.

Online version checking starts asynchronously and refreshes local detection before comparing upstream releases. Reuse recent successful upstream metadata while still refreshing installed versions. Publish independent provider results promptly and expose actionable sanitized errors. A recognized vendor stable build can match the upstream release while vendor package revisions remain outside that comparison. Nightly/Git/prerelease/unrecognized builds receive explicit uncertain states.

Installing into `bin/` updates an app-local copy. It does not replace or activate over a working PATH or explicit executable. Version UI explains the appropriate update route instead of implying that a download changed a different installation.

## State ownership and installation lifecycle

The source/CMD launcher stores presets, preferences, jobs, and installed engines under its own runtime folder. The browser is a client: it holds a temporary authentication cookie and current form state, not the persistent preset database. A frozen executable uses a dedicated user-data folder separate from bundled assets. Old browser presets require an explicit import or clear action. Saves use a validated, bounded JSON store, atomic replacement, and revision checks so stale windows cannot overwrite newer changes.

Installation status belongs beside process status, not only inside Settings. Reopening the page reads the current worker state. Show measured download progress and elapsed time, indicate unknown totals, and expose cancellation throughout the operation. Keep cancellation pending until staged files are cleaned up; never unblock replacement-sensitive jobs early. Installing an inactive portable alternative may permit jobs using external engines. Prevent path changes until installation ends.

Use direct technical labels: Size limit, Lock resolution, Lock FPS, Download, Cancel, and Reload. Explanatory text should describe a constraint or operation, not reassure or advertise.

## Hardening boundaries

Ordinary launch binds to loopback. API access requires a browser session and matching host/origin; source previews serve registered media, and thumbnail routes are constrained to job resources. Request bodies have limits, finite JSON, unambiguous framing, and read deadlines. Rejected bodies close the connection. Query strings are not logged. Expected client disconnects, including Windows aborted connections, do not trigger a second response or a traceback.

Encoder processes receive argument arrays without a shell. Logs and output tails are bounded; cancellation escalates from terminate to kill. Executable probes run outside the job-state lock and concurrent refreshes share a probe batch. Input media is not overwritten. Dependency downloads use fixed HTTPS providers, size limits, SHA-256 verification, safe selective extraction, and staged replacement. No background update installation occurs.

Custom filters and executable selection are trusted local-operator capabilities, not a multi-user sandbox. Do not expose the server to untrusted networks. Temporary media, configuration, caches, and logs are excluded from Git.

## Validation

Engine tests cover candidate ranking, pins, exact-size acceptance, estimates, resource limits, and cancellation. HTTP tests cover session/origin restrictions, framing, malformed input, file access, uploads, ranges, and thumbnail access. Dependency tests use controlled metadata/downloads and archive fixtures. CI covers Windows and Linux with Python 3.10, 3.13, and 3.14; JavaScript syntax is checked separately. Browser QA should exercise the three modes, saved/imported recipes, pinned no-fit, responsive layout, history, logs, toolbar states, and dependency diagnostics.

This matrix does not establish compatibility with every external encoder build or prove general speed/quality improvements. Evaluate performance with representative clips and measured complete outputs; cached estimates and synthetic sources alone cannot establish those claims.
