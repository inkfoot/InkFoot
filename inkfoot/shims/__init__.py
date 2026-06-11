"""SDK shim package — Pattern A instrumentation.

This package monkey-patches the Anthropic and OpenAI SDK call paths
so every LLM call lands an event in the storage layer. The
acceptance contract is **never raise into user code**:
every hook is wrapped in :func:`_isolation.safely_run` or the
:func:`_isolation.isolated_hook` decorator; any exception from our
own code logs at ``WARNING`` and returns control to the original
SDK path.
"""
