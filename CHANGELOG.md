# Changelog

## 1.5.0

- Store saved presets and interface preferences in the application runtime folder. Source/CMD launches are portable; frozen Windows launches use LocalAppData. Legacy browser presets require explicit import or clearing.
- Display dependency installation status in the activity column, with cancellation, download progress, and bounded waits. An inactive portable alternative does not block encoding through available external tools.
- Use direct technical interface labels for mode selection, size limits, and resolution/FPS locks.
- Fix Windows short-path handling in thumbnail generation and canonical-path test assertions. Add short-name and junction regression coverage.
- Add contribution guidance and issue/PR templates.

## 1.4.0

- Consolidate encoding into Default, Smart, and Advanced modes.
- Add Smart resolution/FPS constraints, editable size shortcuts, saved mode-aware recipes, and history thumbnails.
- Refresh active PATH tool versions before upstream checks and recognize stable vendor build banners.

## 1.3.0

- Add live process telemetry, bounded logs, improved cancellation, and FFmpeg performance controls.
- Move process status, results, and history into the activity column and estimates into the fixed bottom toolbar.
- Harden dependency detection, installation, and update diagnostics.

## 1.2.1

- Handle interrupted browser connections on Windows without a second HTTP response or traceback.
