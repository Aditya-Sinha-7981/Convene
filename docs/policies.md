# Policy repository

CON-17 stores local policy documents as append-only versions. PDF and DOCX uploads use `multipart/form-data` (the single exception to `api.md`'s JSON-only body rule); the configured policy-file limit applies instead of the 64 KiB JSON limit. Upload returns `202` with `pending`; local extraction and embedding run in background at the existing compute priority, never on the STT path. PDF extraction reads only the embedded text layer. A scanned/image-only PDF fails visibly with `no_text_layer`; Convene performs no OCR.

`POST /api/qa` remains meeting-only when scope is omitted. A future UI may opt into `Policies` or `Both`; in Both mode each index applies its own threshold and per-source cap before evidence is interleaved—scores from distinct indexes are never globally merged. Policy citations use `source_type: "policy"`; meeting citations carry additive `source_type: "meeting"` by default.
