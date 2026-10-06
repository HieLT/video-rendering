# Dola Pro Engine (Chromium Extension)

This extension provides duration unlock (`15s`, `30s`) and master unwatermarked resource extraction for Dola and Doubao.

## Key Notes
1. **Incognito Mode**: If running in incognito or headless browser profiles, enable "Allow in Incognito" in `chrome://extensions/`.
2. **Prompt Guidelines**: When starting a new conversation for 30s generation, avoid putting duration words (e.g. "30s", "30 seconds") directly in the prompt text. The engine attaches duration capability via protocol.
3. **Mock Data**: `dola-skill-pack-response.json` and `doubao-skill-pack-response.json` are intercepted by the debugger to inject duration parameters.

## Legacy and combined video settings

The action-bar response patch supports both legacy duration `option_list` menus
and combined ratio/duration panels. For the latter it extends duration templates
(`lower_bound`, `upper_bound`, `step_length`) and `supported_duration_range`
(`lower`, `upper`, `step`) to 30 seconds when 30 lies on the existing step grid.
Other controls, defaults, and larger ranges are preserved. JSON-encoded nested
templates and plain JSON objects are both supported.

The UI worker closes persistent ratio panels explicitly, reads seconds from
combined labels, and supports both option menus and sliders. Indexed sliders are
operated with keyboard events and checked against the committed duration label.
Unavailable durations raise an error rather than submitting a shorter video.

After updating, restart the gateway and reopen account browsers so Chromium
loads the updated extension. Release 1.0.2 also changes the background entrypoint
to `service-worker-v2.js`: existing profiles were observed running cached old
JavaScript even while reporting manifest version 1.0.1. When replacing background
code in affected persistent profiles, change the entrypoint as well as the version;
verify the running worker, not just the manifest version.

Offline checks (from the repository root):

```text
python test_video_duration.py
python test_video_duration_formats.py
python test_video_ratio.py
python test_ratio_recovery.py
node test_dola_duration_extension.cjs
```
