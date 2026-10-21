---
name: reference-selection
description: Select one visually verified Film Studio reference candidate from a batch for independent review.
---

# Reference candidate selection

Compare the five actual candidate images in their supplied order with the reference specification, locked color, repair notes and any supplied identity reference. For characters, inspect the required full-body and close-up angles, identity, costume, mask, distinctive features and applicable props. Never infer an unseen detail or unmask a character whose face is not specified.

Select the strongest candidate that visibly satisfies the selection criteria and provide its one-based index; the independent reference reviewer makes the final decision. The selector is a ranking step, not the final approval gate: do not withhold a selection merely because a contact sheet cannot prove every close-up detail at original resolution. When one or more candidates visibly meet the requested silhouette, composition, locked color, and required panel structure, select the strongest one and describe the remaining detail for independent QA. Do not select an index recorded in any supplied `candidate_rejection` input: it has already failed independent inspection. Review feedback is evidence about that candidate, not permission to relax the specification.

Return FAIL only when actual visual inspection supports rejecting every remaining candidate and the previously excluded candidates have independent failure reports. Return NOT_INSPECTED or NEEDS_REVIEW only when the candidate images themselves are absent, unreadable, or not visually distinguishable. Never use NEEDS_REVIEW only because original-resolution inspection belongs to independent QA. Never treat missing evidence as an image defect. Do not invent IDs, alter the story, or approve the whole reference set.
