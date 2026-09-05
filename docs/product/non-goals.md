# Non-goals and boundaries

The following are not default VisualWorld capabilities:

- identifying anonymous visual people by real-world name;
- claiming exact physical distance, speed, or size without calibration;
- treating a detector output, OCR string, or model statement as ground truth;
- forcing a binary identity decision when evidence is insufficient;
- invoking an expensive VLM on every frame during ingestion;
- coupling core world/query logic to one model vendor or runtime;
- uploading local media or derived sensitive data without explicit opt-in;
- committing private/copyrighted media, model weights, or large datasets;
- building distributed infrastructure before a measured requirement exists;
- executing SQL, shell commands, paths, or URLs supplied by model or media text.

VisualWorld can produce anonymous face crops as evidence only when the user has
the right to process the source. Cross-recording human ReID and plate workflows
require explicit privacy safeguards and remain disabled until designed and
approved.
