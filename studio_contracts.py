"""Versioned agent contracts. No model output is trusted without validation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Agent-to-agent communication is mediated by the Workflow orchestrator.  These
# values are also written into every task envelope so a worker can reject stale
# or misrouted messages instead of treating prose as a result.
PROTOCOL_VERSION = 1
MESSAGE_TYPE = 'film_studio.task'
DELIVERY_STATES = ('not_sent', 'sending', 'sent', 'received', 'invalid_result', 'uncertain', 'blocked')


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Issue(Contract):
    severity: Literal['CRITICAL', 'MAJOR', 'MINOR', 'OPTIONAL']
    evidence: str = Field(min_length=1)
    description: str = Field(min_length=1)
    fix: str = Field(min_length=1)


class ScriptReview(Contract):
    verdict: Literal['PASS', 'NEEDS_REVIEW', 'NOT_INSPECTED']
    summary: str = Field(min_length=1)
    issues: list[Issue]


class ScriptEdit(Contract):
    screenplay: str = Field(min_length=1, max_length=500000)
    changes: list[str] = Field(min_length=1)


class ClipPlan(Contract):
    number: int = Field(ge=1, le=1000)
    chapter: int = Field(ge=1, le=1000)
    source_excerpt: str = Field(min_length=1)
    action: str = Field(min_length=1)
    dialogue: str
    color: str = Field(min_length=1)
    start_state: str = Field(min_length=1)
    end_state: str = Field(min_length=1)
    depends_on: list[int] = Field(default_factory=list)


class ProductionPlan(Contract):
    clips: list[ClipPlan] = Field(min_length=1, max_length=1000)
    coverage_notes: str = Field(min_length=1)

    @model_validator(mode='after')
    def sequence(self):
        if [c.number for c in self.clips] != list(range(1, len(self.clips) + 1)):
            raise ValueError('Video numbers must be continuous from 1.')
        for clip in self.clips:
            if len(clip.depends_on) != len(set(clip.depends_on)) or any(n < 1 or n >= clip.number for n in clip.depends_on):
                raise ValueError('Dependencies must reference earlier clips without duplicates.')
        return self


class ReferenceSpec(Contract):
    id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    description: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)
    reference_type: Literal['character', 'character_group', 'environment', 'prop', 'style', 'effect', 'other']


class ReferencePlan(Contract):
    references: list[ReferenceSpec] = Field(min_length=1, max_length=500)
    clip_references: dict[str, list[str]]

    @model_validator(mode='after')
    def unique(self):
        ids = [r.id for r in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate reference ID.')
        for values in self.clip_references.values():
            if not values or len(values) != len(set(values)) or set(values) - set(ids):
                raise ValueError('Invalid reference mapping.')
        return self


class ImageResult(Contract):
    image_id: str = Field(pattern=r'^[a-f0-9]{64}$')


class CharacterSheetChecks(Contract):
    full_body_front_side_back: bool
    face_front_three_quarter_side_back: bool
    consistent_identity_and_costume: bool
    distinctive_details_and_props: bool


class CandidateSelection(Contract):
    verdict: Literal['PASS', 'FAIL', 'NOT_INSPECTED', 'NEEDS_REVIEW']
    image_inspected: bool
    selected_candidate: int | None = Field(default=None, ge=1, le=5)
    evidence: str = Field(min_length=1)
    repair: str = ''
    character_sheet_checks: CharacterSheetChecks | None = None

    @model_validator(mode='after')
    def selection_requires_inspection(self):
        if self.verdict == 'PASS' and (not self.image_inspected or self.selected_candidate is None):
            raise ValueError('PASS requires inspecting and selecting a candidate.')
        if self.verdict == 'FAIL' and not self.image_inspected:
            raise ValueError('FAIL requires inspecting the candidates.')
        if self.verdict != 'PASS' and self.selected_candidate is not None:
            raise ValueError('Only PASS may select a candidate.')
        return self


class ReferenceVerdict(Contract):
    reference_id: str
    verdict: Literal['PASS', 'FAIL', 'NOT_INSPECTED', 'NEEDS_REVIEW']
    image_inspected: bool
    evidence: str = Field(min_length=1)
    repair: str = ''
    character_sheet_checks: CharacterSheetChecks | None = None

    @model_validator(mode='after')
    def actual_image(self):
        if self.verdict in ('PASS', 'FAIL') and not self.image_inspected:
            raise ValueError('Inspect the real image before judging it.')
        return self


class ReferenceQA(Contract):
    references: list[ReferenceVerdict] = Field(min_length=1)


class PromptResult(Contract):
    prompt: str = Field(min_length=1, max_length=30000)
    reference_ids: list[str] = Field(min_length=1)
    story_preserved: Literal[True]
    color_preserved: Literal[True]
    skill_sources: dict[str, str] = Field(default_factory=dict)
    reference_audit: str = Field(min_length=1)
    shot_plan: str = Field(min_length=1)
    production_review: str = Field(min_length=1)
    final_qa: str = Field(min_length=1)
    next_video_handoff: str = Field(min_length=1)


class VideoQA(Contract):
    verdict: Literal['PASS', 'FAIL', 'NOT_INSPECTED', 'NEEDS_REVIEW']
    video_inspected: bool
    audio_inspected: bool
    screenplay_match: bool | None
    color_match: bool | None
    evidence: list[str] = Field(min_length=1)
    repair: str = ''

    @model_validator(mode='after')
    def observed(self):
        if self.verdict in ('PASS', 'FAIL') and not self.video_inspected:
            raise ValueError('Actual video inspection is required.')
        if self.verdict == 'PASS' and (self.screenplay_match is not True or self.color_match is not True):
            raise ValueError('PASS requires screenplay and color match.')
        if self.verdict == 'FAIL' and self.screenplay_match is not False and self.color_match is not False:
            raise ValueError('FAIL requires a concrete screenplay or color mismatch.')
        return self


OUTPUTS = {'script_review': ScriptReview, 'script_edit': ScriptEdit,
           'production_plan': ProductionPlan, 'reference_plan': ReferencePlan,
           'image_create': ImageResult, 'reference_candidate_qa': CandidateSelection, 'reference_selected_qa': ReferenceVerdict, 'reference_qa': ReferenceQA,
           'prompt': PromptResult, 'prompt_repair': PromptResult, 'video_qa': VideoQA}

ROLE_BY_KIND = {'script_review': 'script_doctor', 'script_edit': 'script_editor',
                'production_plan': 'director', 'reference_plan': 'reference_planner',
                'image_create': 'reference_creator', 'reference_candidate_qa': 'reference_selector', 'reference_selected_qa': 'reference_qa', 'reference_qa': 'reference_qa',
                'prompt': 'prompt_engineer', 'prompt_repair': 'prompt_engineer', 'video_qa': 'video_qa'}

INSTRUCTIONS = {
    'script_review': ('Review the supplied complete screenplay without rewriting it. Return PASS when there are no CRITICAL or MAJOR screenplay defects; '
                      'MINOR or OPTIONAL production notes do not block the screenplay gate. Do not treat approval metadata as a screenplay defect. '
                      'Return NEEDS_REVIEW only for concrete CRITICAL or MAJOR screenplay defects, or NOT_INSPECTED when the screenplay is unavailable. '
                      'Cite concrete evidence and report in Vietnamese.'),
    'script_edit': ('Return one complete, consolidated screenplay containing every existing scene. Apply only the requested corrections and preserve all '
                    'unrelated story content, dialogue, scene order and ending. Never return a proposal, status report, approval request, patch, excerpt, '
                    'scene list, or revision notes in place of the screenplay. List the applied changes separately in the changes field.'),
    'production_plan': 'Split the full approved screenplay into ordered 30-second clips, continuously numbered from 1. Preserve all events. Quote a nonempty exact source excerpt per clip. Specify English dialogue, color, start/end states, and dependencies on prior selected takes. Report coverage; do not silently omit content.',
    'reference_plan': 'Design reusable character, costume, prop, environment and state references for the approved plan. Set reference_type for each item: character for each individual recurring character, character_group for crowds, and the appropriate other category. Character references must be multi-panel character sheets, with full-body front, left, back, right and three-quarter views, close-up head/face front, both three-quarter, both side and back views, and clearly visible costume, distinctive details and relevant script-specified accessories or weapons. Preserve masks and never invent unmasked faces, weapons or story elements. Specify actual English image prompts and map every clip number to references. Color follows the screenplay and clip plan.',
    'image_create': 'Create the actual reference image using the project Image Studio from this specification. Return the image ID issued by the application upload step, never invent one. Follow locked identity and color requirements.',
    'reference_candidate_qa': 'Inspect the five candidate images in attachment order against the supplied reference specification and repair notes. Additional image_create inputs are identity reference images attached after the five candidates, in input order. This is a selection step; independent Reference QA is the final approval gate. Choose the strongest visibly suitable candidate whenever candidates are readable: return PASS, image_inspected true, selected_candidate 1-5 and concrete evidence. Do not withhold a selection merely because a contact sheet cannot prove every original-resolution close-up detail; state that limitation for independent QA. For reference_type character, verify the visibly available body/face panel structure, identity, costume and distinctive features, but do not require full-resolution proof of every detail at this stage. Do not select a candidate rejected by supplied candidate_rejection evidence. Return FAIL only when all five are actually inspected and fail. Return NOT_INSPECTED or NEEDS_REVIEW only when the candidates themselves are absent, unreadable or not visually distinguishable. Do not infer image content.',
    'reference_selected_qa': 'Independently inspect the selected candidate image, supplied first, against the reference specification and selection evidence. Additional images are identity references. Do not assess other candidates or infer image content. For character sheets check every required body and face angle, identity, costume, mask and relevant props; PASS requires all character_sheet_checks true. Return FAIL only with concrete observed defects; otherwise use NOT_INSPECTED or NEEDS_REVIEW. Give evidence and targeted repair.',
    'reference_qa': 'Inspect every actual supplied image against its specification. For reference_type character, inspect the character sheet and fill character_sheet_checks: full-body front/sides/back, head/face front/three-quarter/sides/back, consistent identity and costume, and distinctive details plus applicable accessories or weapons. PASS requires every check true. Masks must remain on when required. If detail is too small to inspect, use NOT_INSPECTED or NEEDS_REVIEW. Cite evidence and targeted repairs. Human approval of the whole set follows PASS.',
    'prompt': 'Compile the approved clip and approved references into one English Dola/Seedance prompt. Preserve English dialogue, Dola audio, story and color. Use supplied skill sources only; report their exact hashes. Do not invent references or change the contract. Technical duration/ratio are separate request parameters.',
    'prompt_repair': 'Review the five failed take reports and revise only the prompt expression. Preserve screenplay, approved images, reference order, color and clip duration. Do not lower QA criteria. Use supplied skill sources and their hashes.',
    'video_qa': 'Inspect the actual video and, for dialogue, its audio. Judge ONLY screenplay adherence and locked color. Accept minor irrelevant differences. Evidence needs timestamps. If video/audio cannot be accessed, return NOT_INSPECTED or NEEDS_REVIEW; never fabricate observations.',
}
