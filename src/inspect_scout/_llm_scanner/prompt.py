DEFAULT_SCANNER_TEMPLATE = """
You are an expert in LLM transcript analysis. Here is an LLM transcript you will be analyzing to answer a question:

[BEGIN TRANSCRIPT]
===================================
{{ messages }}
===================================
[END TRANSCRIPT]

{{ answer_prompt }}

{{ question }}

Your answer should include an explanation of your assessment. It should include the message id's (e.g. '[M2]') to clarify which message(s) you are referring to.

{{ answer_format }}
"""


DEFAULT_COHORT_TEMPLATE = """
You are an expert in LLM transcript analysis. Below are {{ cohort_size }} transcripts of different attempts at the SAME task (e.g. different models, agents, or epochs). Each transcript is labeled T1, T2, ... and each of its messages is labeled like [T1:M3].

[BEGIN TRANSCRIPTS]
===================================
{{ transcripts }}
===================================
[END TRANSCRIPTS]

{{ answer_prompt }}

{{ question }}

Compare the transcripts against one another. Your answer should include an explanation that cites specific messages using their labels (e.g. '[T2:M5]') to identify the decision points or chokepoints where the attempts diverged.

{{ answer_format }}
"""


ANSWER_FORMAT_PREAMBLE = (
    "The last line of your response should be of the following format:\n\n"
)

BOOL_ANSWER_PROMPT = (
    "Answer the following yes or no question about the transcript above:"
)
BOOL_ANSWER_FORMAT = (
    ANSWER_FORMAT_PREAMBLE
    + "'ANSWER: $VALUE' (without quotes) where $VALUE is yes or no."
)

NUMBER_ANSWER_PROMPT = (
    "Answer the following numeric question about the transcript above:"
)
NUMBER_ANSWER_FORMAT = (
    ANSWER_FORMAT_PREAMBLE
    + "'ANSWER: $NUMBER' (without quotes) where $NUMBER is the numeric value."
)

LABELS_ANSWER_PROMPT = (
    "Answer the following multiple choice question about the transcript above:"
)
LABELS_ANSWER_FORMAT_SINGLE = (
    ANSWER_FORMAT_PREAMBLE
    + "'ANSWER: $LETTER' (without quotes) where $LETTER is one of {{ letters }} representing:\n{{ formatted_choices }}"
)
LABELS_ANSWER_FORMAT_MULTI = (
    ANSWER_FORMAT_PREAMBLE
    + "'ANSWER: $LETTERS' (without quotes) where $LETTERS is a comma-separated list of letters from {{ letters }} representing:\n{{ formatted_choices }}"
)

STR_ANSWER_PROMPT = "Answer the following question about the transcript above:"
STR_ANSWER_FORMAT = (
    ANSWER_FORMAT_PREAMBLE
    + "'ANSWER: $TEXT' (without quotes) where $TEXT is your answer."
)
