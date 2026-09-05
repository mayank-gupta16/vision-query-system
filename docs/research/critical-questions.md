# Critical research questions

Before v0.2:

1. Which detector/runtime maximizes relevant accuracy per CPU-second on CPU-LITE?
2. Which sampling rate preserves important moving objects, and when should it adapt?
3. How much does original 4K cropping improve plate/face/detail tasks after resized detection?
4. Which tracker is most robust to moving vlog cameras and cuts?
5. Which core, codec, model, and dataset licenses permit the intended distribution/use?

Before v0.3 and v0.6:

6. Which ReID evidence minimizes false merges and duplicate unique-object counts?
7. How should long-gap merge confidence and count bounds be calibrated?
8. How should uncertainty propagate through entity, attribute, event, and query results?

Before v0.4 and v0.5:

9. When does a VLM add measurable value beyond specialist models?
10. How much compute does lazy query-time semantic evaluation save?
11. Which attributes are cheap/reliable enough for ingestion, and which stay query-time?
12. How should multi-frame OCR alternatives be fused without inventing characters?

Security/privacy research required before relevant implementation:

13. What process isolation contains hostile media within CPU-LITE constraints?
14. Which typed query AST and limits withstand SQL/path/URL/rendering injection?
15. Which artifact formats and loading policy prevent model-supply-chain execution?
16. What legally redistributable tiny video/annotation set can serve as CI evidence?
17. Which responsible-use boundaries apply to CCTV, plates, and persistent entities?

Every question must move to a milestone issue with an explicit experiment before
work begins; this list is not a substitute for GitHub tracking.
