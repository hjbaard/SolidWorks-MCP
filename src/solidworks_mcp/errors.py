"""Error types surfaced to the agent."""


class SolidWorksError(RuntimeError):
    """A readable, agent-facing failure from a SolidWorks operation.

    Raised instead of letting raw COM errors / stack traces escape, so the
    agent receives a structured message it can act on in the correction loop.
    """
