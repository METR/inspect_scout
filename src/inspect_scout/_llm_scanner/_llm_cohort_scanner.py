from typing import Any, Callable, Literal

from inspect_ai.model import (
    Model,
    ModelConfig,
    get_model,
)
from inspect_ai.model._model_config import model_config_to_model, model_to_model_config
from inspect_ai.scorer import ValueToFloat
from jinja2 import Environment

from inspect_scout._llm_scanner.structured import structured_generate, structured_schema
from inspect_scout._util.jinja import StrictOnUseUndefined
from inspect_scout._util.refusal import generate_retry_refusals

from .._scanner.cohort_input import Cohort
from .._scanner.extract import MessagesPreprocessor, transcripts_as_str
from .._scanner.result import Result
from .._scanner.scanner import SCANNER_NAME_ATTR, Scanner, scanner
from .._transcript.types import Transcript
from .answer import Answer, answer_from_argument
from .prompt import DEFAULT_COHORT_TEMPLATE
from .types import AnswerMultiLabel, AnswerStructured

CohortTemplateVariables = dict[str, Any] | Callable[[Cohort], dict[str, Any]] | None


@scanner(group_by=["task_set", "task_id"], messages="all")
def llm_cohort_scanner(
    *,
    question: str,
    answer: Literal["boolean", "numeric", "string"]
    | list[str]
    | AnswerMultiLabel
    | AnswerStructured = "string",
    value_to_float: ValueToFloat | None = None,
    template: str | None = None,
    template_variables: CohortTemplateVariables = None,
    preprocessor: MessagesPreprocessor[Transcript] | None = None,
    model: str | Model | None = None,
    retry_refusals: bool | int = 3,
    name: str | None = None,
) -> Scanner[Cohort]:
    """Create a cohort scanner that uses an LLM to compare a group of transcripts.

    This is the cohort analog of `llm_scanner`: instead of analyzing a single
    transcript, it presents a *group* of transcripts (attempts at the same task,
    e.g. across models or epochs) to an LLM so it can root-cause why some
    succeeded and others failed and identify the chokepoints where they diverged.

    The cohort is grouped by `(task_set, task_id)` by default (the same task
    across models). The scope can be overridden per-scanner in a scan job or
    `scout.yaml`.

    Args:
        question: Question for the scanner to answer about the cohort.
        answer: Specification of the answer format. Pass "boolean", "numeric", or
            "string" (default) for a simple answer; `list[str]` for labels;
            `AnswerMultiLabel` for multi-classification; or `AnswerStructured`.
        value_to_float: Optional function to convert the answer value to a float.
        template: Overall template for the scanner prompt. Should include the
            variables `{{ transcripts }}`, `{{ question }}`, `{{ answer_prompt }}`,
            `{{ answer_format }}` (and may use `{{ cohort_size }}`). Defaults to a
            comparison-oriented template.
        template_variables: Additional template variables (or a function of the
            cohort returning them).
        preprocessor: Transform/filter messages of each member before analysis.
        model: Optional model specification (defaults to the scan's model).
        retry_refusals: Retry model refusals (int count, or False to disable).
        name: Scanner name (use when passing this scanner directly to `scan()`).

    Returns:
        A `Scanner` that analyzes a `Cohort` and returns a `Result` (with
        cross-transcript references) summarizing the comparison.
    """
    if template is None:
        template = DEFAULT_COHORT_TEMPLATE
    resolved_answer = answer_from_argument(answer)

    retry_refusals = (
        retry_refusals
        if isinstance(retry_refusals, int)
        else 3
        if retry_refusals is True
        else 0
    )

    # Convert Model instances to serializable ModelConfig so the closure survives
    # cloudpickle roundtrips in multiprocess scanning.
    serializable_model: str | ModelConfig | None
    if isinstance(model, Model):
        serializable_model = model_to_model_config(model)
    else:
        serializable_model = model

    async def scan(cohort: Cohort) -> Result:
        resolved_model: str | Model | None = (
            model_config_to_model(serializable_model)
            if isinstance(serializable_model, ModelConfig)
            else serializable_model
        )

        transcripts_str, extract_references = await transcripts_as_str(
            cohort.transcripts, preprocessor=preprocessor
        )

        resolved_prompt = render_cohort_prompt(
            template=template,
            template_variables=template_variables,
            cohort=cohort,
            transcripts=transcripts_str,
            question=question,
            answer=resolved_answer,
        )

        if isinstance(answer, AnswerStructured):
            value, _, model_output = await structured_generate(
                input=resolved_prompt,
                schema=structured_schema(answer),
                answer_tool=answer.answer_tool,
                model=resolved_model,
                max_attempts=answer.max_attempts,
                retry_refusals=retry_refusals,
            )
            if value is None:
                return Result(
                    value=None,
                    answer=model_output.completion,
                    metadata={"stop_reason": model_output.stop_reason},
                )
        else:
            model_output = await generate_retry_refusals(
                get_model(resolved_model),
                resolved_prompt,
                tools=[],
                tool_choice=None,
                config=None,
                retry_refusals=retry_refusals,
            )

        result = resolved_answer.result_for_answer(
            model_output, extract_references, value_to_float
        )
        result.metadata = {
            **(result.metadata or {}),
            "stop_reason": model_output.stop_reason,
        }
        return result

    if name is not None:
        setattr(scan, SCANNER_NAME_ATTR, name)

    return scan


def render_cohort_prompt(
    *,
    template: str,
    template_variables: CohortTemplateVariables = None,
    cohort: Cohort,
    transcripts: str,
    question: str,
    answer: Answer,
) -> str:
    """Render a cohort scanner prompt template.

    Args:
        template: Jinja2 template string for the cohort prompt.
        template_variables: Additional variables (or a function of the cohort).
        cohort: The cohort being scanned.
        transcripts: The rendered transcripts string (with `[T#:M#]` labels).
        question: Question for the scanner to answer.
        answer: Answer object providing prompt/format strings.

    Returns:
        Rendered prompt string.
    """
    resolved_vars = template_variables or {}
    if callable(resolved_vars):
        resolved_vars = resolved_vars(cohort)

    return (
        Environment(undefined=StrictOnUseUndefined)
        .from_string(template)
        .render(
            transcripts=transcripts,
            cohort_size=len(cohort.members),
            question=question,
            answer_prompt=answer.prompt,
            answer_format=answer.format,
            **resolved_vars,
        )
    )
